"""Pure-stdlib statistical primitives for the retrieval benchmark and the
cross-store certifier.

No scipy, no numpy: every function takes only integer/float counts, never store
text, so the whole module is safe to exercise in CI. ``retrieval_bench.py`` and
``pool_certify.py`` both import from here; keeping the Wilson / Clopper-Pearson /
homogeneity math in one place means the per-store measurement and the cross-store
pool cannot drift apart. The per-store primitives (Wilson, Clopper-Pearson, the
rule of three, exact McNemar, Holm) were factored out of ``retrieval_bench.py``
unchanged; the homogeneity test and the beta-binomial bound are added for the
cross-store pool.

Every proportion m/n is reported with an asymmetric lower bound; the gate reads
the LOWER bound, never the point estimate.
"""

from __future__ import annotations

import math

# 97.5th percentile of the standard normal (two-sided 95%).
_Z_95 = 1.959963984540054


def wilson_lower(k: int, n: int, z: float = _Z_95) -> float:
    """Wilson score lower bound for a binomial proportion k/n.

    Closed-form; the asymmetric interval the small-N gate needs. Returns 0.0
    for n == 0 (no evidence bounds nothing).
    """
    if n == 0:
        return 0.0
    p = k / n
    z2 = z * z
    denom = 1.0 + z2 / n
    center = p + z2 / (2 * n)
    margin = z * math.sqrt(p * (1 - p) / n + z2 / (4 * n * n))
    return max(0.0, (center - margin) / denom)


def _betacf(a: float, b: float, x: float) -> float:
    """Continued-fraction expansion for the incomplete beta (Lentz)."""
    fpmin = 1e-300
    qab, qap, qam = a + b, a + 1.0, a - 1.0
    c = 1.0
    d = 1.0 - qab * x / qap
    if abs(d) < fpmin:
        d = fpmin
    d = 1.0 / d
    h = d
    for m in range(1, 201):
        m2 = 2 * m
        aa = m * (b - m) * x / ((qam + m2) * (a + m2))
        d = 1.0 + aa * d
        if abs(d) < fpmin:
            d = fpmin
        c = 1.0 + aa / c
        if abs(c) < fpmin:
            c = fpmin
        d = 1.0 / d
        h *= d * c
        aa = -(a + m) * (qab + m) * x / ((a + m2) * (qap + m2))
        d = 1.0 + aa * d
        if abs(d) < fpmin:
            d = fpmin
        c = 1.0 + aa / c
        if abs(c) < fpmin:
            c = fpmin
        d = 1.0 / d
        delta = d * c
        h *= delta
        if abs(delta - 1.0) < 3e-16:
            break
    return h


def _betai(a: float, b: float, x: float) -> float:
    """Regularized incomplete beta I_x(a, b) via the continued fraction."""
    if x <= 0.0:
        return 0.0
    if x >= 1.0:
        return 1.0
    lbeta = math.lgamma(a) + math.lgamma(b) - math.lgamma(a + b)
    front = math.exp(a * math.log(x) + b * math.log(1.0 - x) - lbeta)
    if x < (a + 1.0) / (a + b + 2.0):
        return front * _betacf(a, b, x) / a
    return 1.0 - front * _betacf(b, a, 1.0 - x) / b


def _beta_ppf(p: float, a: float, b: float) -> float:
    """Inverse of the regularized incomplete beta by bisection.

    Stdlib-only: scipy's ``beta.ppf`` is the conventional route, but the
    benchmark holds its dependency footprint to compute-only kernel deps, so
    the Clopper-Pearson bound is built on this bisection over ``_betai``.
    """
    lo, hi = 0.0, 1.0
    for _ in range(200):
        mid = (lo + hi) / 2.0
        if _betai(a, b, mid) < p:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2.0


def clopper_pearson_lower(k: int, n: int, alpha: float = 0.05) -> float:
    """Conservative (exact) Clopper-Pearson lower bound for k/n.

    The lower limit is the (alpha/2)-quantile of Beta(k, n-k+1); 0.0 at k == 0
    (the rule of three then bounds the true rate, not this interval).
    """
    if n == 0 or k == 0:
        return 0.0
    if k == n:
        # Closed form avoids a bisection edge at the boundary: (alpha/2)^(1/n).
        return (alpha / 2.0) ** (1.0 / n)
    return _beta_ppf(alpha / 2.0, k, n - k + 1)


def rule_of_three_upper(n: int) -> float:
    """Upper bound on a true rate after 0 observed events in n trials (~3/n).

    The honest reading of a clean run: 0/n does not bound the FP rate at 0, only
    at roughly 3/n at 95% confidence. Returns 1.0 for n == 0.
    """
    return 1.0 if n == 0 else min(1.0, 3.0 / n)


def mcnemar_exact_p(b: int, c: int) -> float:
    """Two-sided exact McNemar p-value for discordant pairs (b, c).

    Binomial test on the discordant cells with p = 1/2. Used to report the
    rejected-name-indexing catch moves (44->46, 46->47) as not statistically
    distinguishable: at
    n = 48 the discordant set is 2-4 items, far short of significance.
    """
    n = b + c
    if n == 0:
        return 1.0
    k = min(b, c)
    tail = sum(math.comb(n, i) for i in range(k + 1)) * (0.5**n)
    return min(1.0, 2.0 * tail)


def holm_reject(pvalues: list[float], alpha: float = 0.05) -> list[bool]:
    """Holm step-down multiplicity correction over the operating points searched.

    Returns a reject/keep mask aligned with the input order. Holm dominates
    plain Bonferroni at the same family-wise error rate; both are reported so a
    post-hoc operating-point search cannot manufacture a significant cell.
    """
    m = len(pvalues)
    if m == 0:
        return []
    order = sorted(range(m), key=lambda i: pvalues[i])
    reject = [False] * m
    for rank, idx in enumerate(order):
        if pvalues[idx] <= alpha / (m - rank):
            reject[idx] = True
        else:
            break
    return reject


def required_n_fired(target_lb: float, z: float = _Z_95) -> int:
    """Smallest perfect-run n whose Wilson lower bound reaches ``target_lb``.

    A first-class output: ~120-150 for a 0.975 lower bound; 48/48 caps at
    ~0.926, so the high-stakes tier is years away on a single store. Capped to
    avoid an unbounded loop on an unreachable target.
    """
    for n in range(1, 100001):
        if wilson_lower(n, n, z) >= target_lb:
            return n
    return -1


# ---------------------------------------------------------------------------
# Cross-store primitives: added for pool_certify.py. Homogeneity across stores
# and a random-effects sensitivity bound. Same discipline: counts only, no store
# text, safe in CI.
# ---------------------------------------------------------------------------


def holm_adjusted(pvalues: list[float]) -> list[float]:
    """Holm step-down adjusted p-values, aligned with the input order.

    The multiplicity-corrected companion to ``holm_reject``: a hypothesis is
    rejected at level alpha iff its adjusted p-value is <= alpha. Adjusted values
    are made monotone non-decreasing along the sorted order, so the reject set is
    nested and the two functions agree on the same family.
    """
    m = len(pvalues)
    if m == 0:
        return []
    order = sorted(range(m), key=lambda i: pvalues[i])
    adjusted = [0.0] * m
    running = 0.0
    for rank, idx in enumerate(order):
        running = max(running, (m - rank) * pvalues[idx])
        adjusted[idx] = min(1.0, running)
    return adjusted


def _gser(a: float, x: float) -> float:
    """Series expansion of the regularized lower incomplete gamma P(a, x)."""
    if x <= 0.0:
        return 0.0
    gln = math.lgamma(a)
    ap = a
    total = 1.0 / a
    delta = total
    for _ in range(1000):
        ap += 1.0
        delta *= x / ap
        total += delta
        if abs(delta) < abs(total) * 1e-15:
            break
    return total * math.exp(-x + a * math.log(x) - gln)


def _gcf(a: float, x: float) -> float:
    """Continued fraction for the regularized upper incomplete gamma Q(a, x)."""
    fpmin = 1e-300
    gln = math.lgamma(a)
    b = x + 1.0 - a
    c = 1.0 / fpmin
    d = 1.0 / b
    h = d
    for i in range(1, 1000):
        an = -i * (i - a)
        b += 2.0
        d = an * d + b
        if abs(d) < fpmin:
            d = fpmin
        c = b + an / c
        if abs(c) < fpmin:
            c = fpmin
        d = 1.0 / d
        delta = d * c
        h *= delta
        if abs(delta - 1.0) < 1e-15:
            break
    return math.exp(-x + a * math.log(x) - gln) * h


def chi_square_sf(x: float, df: int) -> float:
    """Upper-tail (survival) probability of the chi-square distribution.

    Regularized upper incomplete gamma Q(df/2, x/2), computed by series below the
    turning point and continued fraction above it (Numerical Recipes routing).
    Stdlib-only replacement for ``scipy.stats.chi2.sf``.
    """
    if x <= 0.0:
        return 1.0
    a = df / 2.0
    y = x / 2.0
    if y < a + 1.0:
        return 1.0 - _gser(a, y)
    return _gcf(a, y)


def _fisher_freeman_halton(counts: list[tuple[int, int]]) -> float:
    """Exact homogeneity p-value for a 2xS table by enumeration of the margins.

    ``counts`` is the per-store (successes, trials) list; the table has two rows
    (success / failure) and one column per store, with all margins fixed. The
    p-value is the summed hypergeometric probability of every table at least as
    extreme (probability <= the observed table's, within a floating tolerance) as
    the observed one. Only reached when a cell's expected count is too small for
    the chi-square approximation, which bounds the enumeration.
    """
    col_sums = [n for _, n in counts]
    r1 = sum(k for k, _ in counts)
    total = sum(col_sums)
    r2 = total - r1
    const = (
        math.lgamma(r1 + 1)
        + math.lgamma(r2 + 1)
        + sum(math.lgamma(c + 1) for c in col_sums)
        - math.lgamma(total + 1)
    )

    def log_prob(top: list[int]) -> float:
        s = const
        for a, c in zip(top, col_sums, strict=True):
            s -= math.lgamma(a + 1) + math.lgamma(c - a + 1)
        return s

    observed = [k for k, _ in counts]
    log_threshold = log_prob(observed) + math.log1p(1e-7)

    n_cols = len(col_sums)
    suffix = [0] * (n_cols + 1)
    for j in range(n_cols - 1, -1, -1):
        suffix[j] = suffix[j + 1] + col_sums[j]

    accumulated = 0.0

    def walk(j: int, remaining: int, acc_penalty: float) -> None:
        nonlocal accumulated
        if j == n_cols:
            if remaining == 0:
                logp = const + acc_penalty
                if logp <= log_threshold:
                    accumulated += math.exp(logp)
            return
        c = col_sums[j]
        lo = max(0, remaining - suffix[j + 1])
        hi = min(c, remaining)
        for a in range(lo, hi + 1):
            penalty = -(math.lgamma(a + 1) + math.lgamma(c - a + 1))
            walk(j + 1, remaining - a, acc_penalty + penalty)

    walk(0, r1, 0.0)
    return min(1.0, accumulated)


def homogeneity_p(counts: list[tuple[int, int]]) -> float:
    """Homogeneity p-value across per-store 2xS outcome tables.

    ``counts`` is the per-store (successes, trials) list. Tests the null that the
    success proportion is identical across all stores, i.e. that the pool is one
    binomial rather than a mix. Uses the Pearson chi-square test of homogeneity
    when every expected cell count clears the asymptotic threshold, and the exact
    Fisher-Freeman-Halton test (enumeration over fixed margins) otherwise. Returns
    1.0 when there is nothing to test (fewer than two informative stores, or a
    degenerate all-success / all-failure margin).
    """
    counts = [(int(k), int(n)) for k, n in counts if n > 0]
    if len(counts) < 2:
        return 1.0
    total = sum(n for _, n in counts)
    r1 = sum(k for k, _ in counts)
    r2 = total - r1
    if r1 == 0 or r2 == 0:
        return 1.0
    min_expected = min(min(r1 * n / total, r2 * n / total) for _, n in counts)
    if min_expected >= 5.0:
        chi2 = 0.0
        for k, n in counts:
            for obs, row_sum in ((k, r1), (n - k, r2)):
                exp = row_sum * n / total
                chi2 += (obs - exp) ** 2 / exp
        return chi_square_sf(chi2, len(counts) - 1)
    return _fisher_freeman_halton(counts)


def _wilson_lower_float(p: float, n: float, z: float = _Z_95) -> float:
    """Wilson score lower bound at a continuous effective sample size n."""
    if n <= 0.0:
        return 0.0
    z2 = z * z
    denom = 1.0 + z2 / n
    center = p + z2 / (2 * n)
    margin = z * math.sqrt(p * (1 - p) / n + z2 / (4 * n * n))
    return max(0.0, (center - margin) / denom)


def beta_binomial_lower(counts: list[tuple[int, int]], z: float = _Z_95) -> float:
    """Random-effects (beta-binomial) lower bound on the pooled success rate.

    A sensitivity read that widens the pooled interval for between-store
    overdispersion, so a pool whose stores disagree is not judged on a
    fixed-effect interval that assumes they are exchangeable draws from one
    binomial. Moment-based and stdlib-only: the quasi-binomial Pearson dispersion
    phi across stores discounts the pooled sample size to an effective n/phi, and
    the Wilson lower bound is taken at that effective n. Reduces to the plain
    Wilson lower bound when the stores are homogeneous (phi <= 1). This is a
    reported sensitivity bound, never a gate; it is not a full posterior.
    """
    counts = [(int(k), int(n)) for k, n in counts if n > 0]
    total = sum(n for _, n in counts)
    if total == 0:
        return 0.0
    k_total = sum(k for k, _ in counts)
    p = k_total / total
    if p <= 0.0 or p >= 1.0 or len(counts) < 2:
        # No usable dispersion signal; fall back to the fixed-effect bound.
        return _wilson_lower_float(p, float(total), z)
    chi2 = sum((k - n * p) ** 2 / (n * p * (1.0 - p)) for k, n in counts)
    phi = max(1.0, chi2 / (len(counts) - 1))
    return _wilson_lower_float(p, total / phi, z)
