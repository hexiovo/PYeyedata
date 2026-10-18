"""Auditable continuous-outcome inference; no GUI imports or design guessing.

See STATISTICAL_METHODS.md for estimands, templates and support boundaries.
"""
from __future__ import annotations

from dataclasses import asdict
from itertools import combinations
import warnings
from typing import Any, Callable

import numpy as np
import pandas as pd
from scipy import special, stats
import scipy
import statsmodels
from statsmodels.stats.multitest import multipletests

from .contracts import AnalysisBundle, CONTRACT_VERSION, DesignSpec, validate_metric_table

METHOD_VERSION = "1.1.0"
SEED = 20261011
BOOTSTRAPS = 5000


class InferenceBlocked(ValueError):
    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)


class AnalysisCancelled(Exception):
    pass


def _check_cancel(cancel):
    if cancel is not None and cancel():
        raise AnalysisCancelled()


def _json(value):
    """Convert scientific scalars and non-finite values to strict JSON values."""
    if isinstance(value, dict):
        return {str(k): _json(v) for k, v in value.items()}
    if isinstance(value, (tuple, list, np.ndarray)):
        return [_json(v) for v in value]
    if isinstance(value, (np.integer,)):
        return int(value)
    if isinstance(value, (np.bool_,)):
        return bool(value)
    if isinstance(value, (float, np.floating)):
        return float(value) if np.isfinite(value) else None
    if value is pd.NA or value is pd.NaT:
        return None
    if isinstance(value, (str, int, bool)) or value is None:
        return value
    return str(value)


def adjust_pvalues(pvalues, method="BY"):
    """Preserve missing tests; correct only finite probabilities in a fixed family."""
    aliases = {"BY": "fdr_by", "BH": "fdr_bh", "Holm": "holm"}
    if method not in aliases:
        raise InferenceBlocked("unknown_correction", "Correction must be BY, BH or Holm")
    p = np.asarray(pvalues, dtype=float)
    valid = np.isfinite(p)
    if np.any((p[valid] < 0) | (p[valid] > 1)):
        raise InferenceBlocked("invalid_p", "Probabilities must be between zero and one")
    result = np.full(p.shape, np.nan)
    if valid.any():
        result[valid] = multipletests(p[valid], method=aliases[method])[1]
    return result


def _minimum(n, label="independent units"):
    if n < 3:
        raise InferenceBlocked("too_few_independent_units", f"Fewer than three {label}; descriptive results only")


def _variance(x):
    v = float(np.var(x, ddof=1))
    if not np.isfinite(v) or v <= 0:
        raise InferenceBlocked("zero_variance", "Zero/non-finite variance prevents the requested inference")
    return v


def bootstrap_ci(samples, estimator: Callable, *, paired=False, clusters=None,
                 method="percentile", cancel=None, resamples=BOOTSTRAPS, seed=SEED):
    """95% CI; independent groups, whole pairs, or whole independent clusters.

    Cluster estimator receives concatenated sampled rows. Distinct copies of a
    repeated cluster MUST be treated as distinct units by that estimator (rmcorr
    uses pre-centred within-unit residuals, so duplicated labels cannot collapse).
    No point-level resampling of time series is supported.
    """
    samples = tuple(np.asarray(s, dtype=float) for s in samples)
    info = {"seed": seed, "resamples": resamples, "confidence_level": .95,
            "method": method, "scheme": "cluster" if clusters is not None else "pairs" if paired else "independent"}
    if resamples != BOOTSTRAPS:
        raise InferenceBlocked("unsupported_resamples", "Validated bootstrap count is 5000")
    if method not in {"percentile", "BCa"}:
        raise InferenceBlocked("bootstrap_method", "Supported CI methods are percentile and BCa")
    if any(len(s) < 3 or not np.isfinite(s).all() for s in samples):
        return (None, None), {**info, "status": "failed", "reason": "too_few_or_nonfinite"}
    if paired and len(set(map(len, samples))) != 1:
        raise InferenceBlocked("pair_length", "Whole-pair bootstrap requires equal-length arrays")
    if clusters is not None:
        if method != "percentile":
            raise InferenceBlocked("cluster_bca_unvalidated", "Only percentile cluster bootstrap is validated")
        labels = np.asarray(clusters)
        if not paired or any(len(s) != len(labels) for s in samples):
            raise InferenceBlocked("cluster_shape", "Cluster bootstrap requires aligned sample arrays")
        units = pd.unique(labels)
        if len(units) < 3:
            return (None, None), {**info, "status": "failed", "reason": "too_few_clusters"}
        blocks = [np.flatnonzero(labels == unit) for unit in units]
        info["n_clusters"] = len(units)
    rng = np.random.default_rng(seed)
    observed = float(estimator(*samples))
    draws = np.empty(resamples)
    for b in range(resamples):
        if b % 100 == 0:
            _check_cancel(cancel)
        if clusters is not None:
            idx = np.concatenate([blocks[i] for i in rng.integers(0, len(blocks), len(blocks))])
            draw = tuple(s[idx] for s in samples)
        elif paired:
            idx = rng.integers(0, len(samples[0]), len(samples[0]))
            draw = tuple(s[idx] for s in samples)
        else:
            draw = tuple(s[rng.integers(0, len(s), len(s))] for s in samples)
        with np.errstate(all="ignore"):
            try:
                draws[b] = estimator(*draw)
            except (ValueError, ZeroDivisionError, FloatingPointError, InferenceBlocked):
                draws[b] = np.nan
    finite = np.isfinite(draws)
    info["finite_resamples"] = int(finite.sum())
    # Dropping invalid replicates silently changes the resampling distribution.
    if not np.isfinite(observed) or not finite.all() or np.ptp(draws) == 0:
        return (None, None), {**info, "status": "failed", "reason": "degenerate_bootstrap"}
    probabilities = np.array([.025, .975])
    if method == "BCa":
        jack = []
        if paired:
            for i in range(len(samples[0])):
                jack.append(estimator(*(np.delete(s, i, axis=0) for s in samples)))
            influence = np.mean(jack) - np.asarray(jack)
        else:
            # Multi-sample BCa: per-sample jackknife influence scaled by n_i.
            influence_parts = []
            for j, sample in enumerate(samples):
                jvals = []
                for i in range(len(sample)):
                    reduced = list(samples)
                    reduced[j] = np.delete(sample, i, axis=0)
                    jvals.append(estimator(*reduced))
                jvals = np.asarray(jvals)
                influence_parts.append((len(sample) - 1) * (np.mean(jvals) - jvals) / len(sample))
            influence = np.concatenate(influence_parts)
        denom = 6 * np.sum(influence ** 2) ** 1.5
        rank = (np.count_nonzero(draws < observed) + .5 * np.count_nonzero(draws == observed)) / resamples
        if not np.isfinite(influence).all() or denom == 0 or rank <= 0 or rank >= 1:
            return (None, None), {**info, "status": "failed", "reason": "degenerate_bca"}
        acceleration = np.sum(influence ** 3) / denom
        z0 = stats.norm.ppf(rank)
        z = stats.norm.ppf(probabilities)
        probabilities = stats.norm.cdf(z0 + (z0 + z) / (1 - acceleration * (z0 + z)))
        if not np.isfinite(probabilities).all() or probabilities[0] >= probabilities[1]:
            return (None, None), {**info, "status": "failed", "reason": "degenerate_bca"}
        info.update(acceleration=float(acceleration), bias_correction=float(z0))
    lo, hi = np.quantile(draws, probabilities)
    return (float(lo), float(hi)), {**info, "status": "complete"}


def _hedges(a, b):
    df = len(a) + len(b) - 2
    sd = np.sqrt(((len(a) - 1) * np.var(a, ddof=1) + (len(b) - 1) * np.var(b, ddof=1)) / df)
    if sd == 0:
        return float("nan")
    correction = np.exp(special.gammaln(df / 2) - special.gammaln((df - 1) / 2)) / np.sqrt(df / 2)
    return float(correction * (np.mean(a) - np.mean(b)) / sd)


def welch_t(a, b, cancel=None, ci_method="percentile"):
    a, b = np.asarray(a, float), np.asarray(b, float)
    _minimum(len(a), "units in the first group")
    _minimum(len(b), "units in the second group")
    va, vb = _variance(a), _variance(b)
    sa, sb = va / len(a), vb / len(b)
    se = np.sqrt(sa + sb)
    df = (sa + sb) ** 2 / (sa ** 2 / (len(a) - 1) + sb ** 2 / (len(b) - 1))
    difference = float(a.mean() - b.mean())
    t = difference / se
    half = stats.t.ppf(.975, df) * se
    eci, boot = bootstrap_ci((a, b), _hedges, cancel=cancel, method=ci_method)
    return dict(method="Welch t", estimate=difference, ci_low=difference-half, ci_high=difference+half,
                ci_method="Welch t interval", statistic=t, statistic_name="t", df=df,
                p=float(2*stats.t.sf(abs(t), df)), effect=_hedges(a, b), effect_name="Hedges g",
                effect_definition="Exact gamma bias correction; pooled sample SD; first minus second group",
                effect_ci_low=eci[0], effect_ci_high=eci[1], bootstrap=boot,
                n_independent=len(a)+len(b), n_observations=len(a)+len(b), n_groups=[len(a),len(b)])


def paired_t(a, b, cancel=None, ci_method="percentile"):
    a, b = np.asarray(a, float), np.asarray(b, float)
    if len(a) != len(b):
        raise InferenceBlocked("unmatched_pairs", "Paired arrays must have the same complete units")
    _minimum(len(a), "complete pairs")
    diff = a-b
    sd = np.sqrt(_variance(diff))
    estimate = float(diff.mean())
    df = len(diff)-1
    se = sd/np.sqrt(len(diff))
    dz = estimate/sd
    eci, boot = bootstrap_ci((a,b), lambda x,y: np.mean(x-y)/np.std(x-y,ddof=1),
                             paired=True, cancel=cancel, method=ci_method)
    half = stats.t.ppf(.975,df)*se
    return dict(method="Paired t", estimate=estimate, ci_low=estimate-half, ci_high=estimate+half,
                ci_method="paired-difference t interval", statistic=estimate/se, statistic_name="t", df=df,
                p=float(2*stats.t.sf(abs(estimate/se),df)), effect=dz, effect_name="dz",
                effect_definition="First minus second condition mean divided by sample SD of paired differences",
                effect_ci_low=eci[0], effect_ci_high=eci[1], bootstrap=boot,
                n_independent=len(a), n_observations=2*len(a))


def welch_anova(groups):
    """Welch (1951) one-way F, including Welch's finite-sample correction."""
    if len(groups)<3:
        raise InferenceBlocked("levels", "Welch ANOVA requires at least three groups")
    for g in groups:
        _minimum(len(g), "units per group")
    n = np.array([len(g) for g in groups],float)
    means = np.array([np.mean(g) for g in groups])
    variances = np.array([_variance(g) for g in groups])
    weights = n/variances
    relative = weights/weights.sum()
    mean = np.dot(relative,means)
    k = len(groups)
    term = np.sum((1-relative)**2/(n-1))
    f = np.dot(weights,(means-mean)**2)/(k-1)/(1+2*(k-2)*term/(k*k-1))
    df1, df2 = k-1, (k*k-1)/(3*term)
    return dict(method="Welch ANOVA", estimate=None, statistic=f, statistic_name="F", df=df1, df_den=df2,
                p=float(stats.f.sf(f,df1,df2)), effect=None, effect_name="group mean contrasts",
                effect_definition="Welch omnibus has no pooled-variance eta-squared claim; Games–Howell reports differences",
                group_means=means.tolist(), n_independent=int(n.sum()), n_observations=int(n.sum()))


def games_howell(groups, labels):
    """Games–Howell all-pair simultaneous studentized-range intervals/p-values."""
    k = len(groups)
    if k<3 or len(labels)!=k:
        raise InferenceBlocked("levels", "Games–Howell needs at least three labelled groups")
    rows=[]
    for i,j in combinations(range(k),2):
        a,b=np.asarray(groups[i],float),np.asarray(groups[j],float)
        _minimum(len(a)); _minimum(len(b))
        sa,sb=_variance(a)/len(a),_variance(b)/len(b)
        df=(sa+sb)**2/(sa**2/(len(a)-1)+sb**2/(len(b)-1))
        se=np.sqrt((sa+sb)/2)
        difference=float(a.mean()-b.mean())
        q=abs(difference)/se
        half=stats.studentized_range.ppf(.95,k,df)*se
        p=float(stats.studentized_range.sf(q,k,df))
        rows.append(dict(method="Games–Howell", contrast=[str(labels[i]),str(labels[j])], estimate=difference,
                         ci_low=difference-half,ci_high=difference+half,ci_method="simultaneous Games–Howell",
                         statistic=q,statistic_name="q",df=df,p=p,p_adj=p,correction="Games–Howell (already simultaneous)",
                         preadjusted=True,effect=difference,effect_name="mean difference",effect_definition="First minus second group",
                         effect_ci_low=difference-half,effect_ci_high=difference+half,
                         n_independent=len(a)+len(b),n_observations=len(a)+len(b)))
    return rows


def repeated_anova(values, cancel=None):
    """Balanced one-factor repeated ANOVA and genuine Greenhouse–Geisser epsilon."""
    x=np.asarray(values,float)
    if x.ndim!=2 or x.shape[1]<3 or not np.isfinite(x).all():
        raise InferenceBlocked("rm_balance", "RM ANOVA needs a finite complete subject × >=3 condition matrix")
    n,k=x.shape
    _minimum(n,"complete subjects")
    grand=x.mean(); subject=x.mean(axis=1,keepdims=True); condition=x.mean(axis=0,keepdims=True)
    ss_condition=float(n*np.sum((condition-grand)**2))
    ss_error=float(np.sum((x-subject-condition+grand)**2))
    if ss_error <= np.finfo(float).eps * max(float(np.sum((x-grand)**2)),np.finfo(float).tiny)*64:
        raise InferenceBlocked("zero_residual_variance", "Zero residual variance prevents repeated-measures inference")
    projector=np.eye(k)-np.ones((k,k))/k
    cov=projector @ np.cov(x,rowvar=False,ddof=1) @ projector
    trace=float(np.trace(cov)); trace2=float(np.trace(cov@cov))
    if trace2<=0:
        raise InferenceBlocked("gg_degenerate", "Greenhouse–Geisser covariance is degenerate")
    epsilon=float(np.clip(trace**2/((k-1)*trace2),1/(k-1),1))
    df1=k-1; df2=(n-1)*(k-1)
    f=(ss_condition/df1)/(ss_error/df2)
    eta=ss_condition/(ss_condition+ss_error)
    def effect(z):
        g=z.mean(); c=z.mean(axis=0,keepdims=True); s=z.mean(axis=1,keepdims=True)
        numerator=len(z)*np.sum((c-g)**2)
        denominator=numerator+np.sum((z-s-c+g)**2)
        return numerator/denominator if denominator>0 else np.nan
    eci,boot=bootstrap_ci((x,),effect,paired=True,cancel=cancel)
    return dict(method="RM ANOVA (Greenhouse–Geisser)",estimate=None,statistic=f,statistic_name="F",
                df=df1*epsilon,df_den=df2*epsilon,df_uncorrected=df1,df_den_uncorrected=df2,
                epsilon=epsilon,p=float(stats.f.sf(f,df1*epsilon,df2*epsilon)),
                p_uncorrected=float(stats.f.sf(f,df1,df2)),effect=eta,effect_name="partial eta squared",
                effect_definition="SS condition / (SS condition + SS within-subject error)",
                effect_ci_low=eci[0],effect_ci_high=eci[1],bootstrap=boot,
                ss_condition=ss_condition,ss_error=ss_error,n_independent=n,n_observations=n*k)


def association(x,y,method="pearson",cancel=None,ci_method="percentile"):
    x,y=np.asarray(x,float),np.asarray(y,float)
    _minimum(len(x))
    if len(x)!=len(y):
        raise InferenceBlocked("association_shape","Association arrays must align")
    _variance(x); _variance(y)
    if method=="pearson":
        result=stats.pearsonr(x,y)
        estimator=lambda a,b: np.corrcoef(a,b)[0,1]
        df=len(x)-2
    elif method=="spearman":
        result=stats.spearmanr(x,y)
        estimator=lambda a,b: np.corrcoef(stats.rankdata(a),stats.rankdata(b))[0,1]
        df=len(x)-2
    else:
        raise InferenceBlocked("association_method","Supported independent targets are Pearson or Spearman")
    ci,boot=bootstrap_ci((x,y),estimator,paired=True,cancel=cancel,method=ci_method)
    r=float(result.statistic)
    return dict(method=method.title(),estimate=r,statistic=r,statistic_name="r" if method=="pearson" else "rho",
                df=df,p=float(result.pvalue),ci_low=ci[0],ci_high=ci[1],ci_method=ci_method+" whole-row bootstrap",
                effect=r,effect_name="r" if method=="pearson" else "rho",effect_ci_low=ci[0],effect_ci_high=ci[1],
                effect_definition="Linear association" if method=="pearson" else "Monotonic rank association (ties averaged)",
                bootstrap=boot,n_independent=len(x),n_observations=len(x),
                assumptions="Independent units; bivariate-normal Pearson null or asymptotic Spearman null (small-N warning)")


def within_association(frame,xcolumn,ycolumn,subjectcolumn,cancel=None):
    """Repeated-measures Pearson correlation with subject-intercept residualization."""
    blocks=[]; excluded=[]
    for subject,part in frame.groupby(subjectcolumn,sort=False):
        if len(part)<2 or part[xcolumn].var(ddof=1)<=0 or part[ycolumn].var(ddof=1)<=0:
            excluded.extend(part.index.tolist())
            continue
        x=part[xcolumn].to_numpy(float);y=part[ycolumn].to_numpy(float)
        blocks.append((subject,x-x.mean(),y-y.mean()))
    _minimum(len(blocks),"subjects with within-subject variation")
    x=np.concatenate([b[1] for b in blocks]);y=np.concatenate([b[2] for b in blocks])
    subjects=np.concatenate([np.full(len(b[1]),i) for i,b in enumerate(blocks)])
    r=float(np.clip(np.dot(x,y)/np.sqrt(np.dot(x,x)*np.dot(y,y)),-1,1))
    if abs(r)>=1-1e-14:
        raise InferenceBlocked("zero_residual_variance","A perfect within-subject relation has zero residual variance; finite rmcorr t inference is unavailable")
    df=len(x)-len(blocks)-1
    if df<1:
        raise InferenceBlocked("rmcorr_df","No residual degrees of freedom")
    t=r*np.sqrt(df/(1-r*r))
    ci,boot=bootstrap_ci((x,y),lambda a,b: np.dot(a,b)/np.sqrt(np.dot(a,a)*np.dot(b,b)),
                         paired=True,clusters=subjects,cancel=cancel)
    return dict(method="Repeated-measures Pearson (rmcorr)",estimate=r,statistic=t,statistic_name="t",df=df,
                p=float(2*stats.t.sf(abs(t),df)),ci_low=ci[0],ci_high=ci[1],ci_method="whole-subject percentile bootstrap",
                effect=r,effect_name="within-subject r",effect_ci_low=ci[0],effect_ci_high=ci[1],
                effect_definition="Pearson correlation of subject-centred residuals; common within-subject slope",
                bootstrap=boot,n_independent=len(blocks),n_observations=len(x),excluded_no_within_variation=excluded,
                retained_subjects=[str(b[0]) for b in blocks],
                assumptions="Subject-specific intercepts, common within slope, independent subjects and conditionally independent homoscedastic residuals; no between-subject interpretation")


def _ids(frame,design):
    columns=[design.get(k) for k in ("subject_column","team_column","interaction_column","trial_column","session_column","analysis_unit_column")]
    return {c: sorted(v for v in frame[c].dropna().astype(str).unique().tolist() if v.strip()) for c in dict.fromkeys(columns) if c and c in frame}


def _audit_exclude(audit,frame,mask,reason):
    selected=frame.loc[mask]
    if len(selected):
        columns=[c for c in ("_source_row","subject_id","session_id","interaction_id","pair_id","trial","condition") if c in selected]
        audit["exclusions"].append({"reason":reason,"n_rows":len(selected),"records":_json(selected[columns].to_dict("records"))})
    return frame.loc[~mask].copy()


def _counts(frame,design):
    ids=_ids(frame,design)
    trials=[design.get(k) for k in ("subject_column","session_column","trial_column")]
    trials=[c for c in trials if c and c in frame]
    return {"rows":len(frame),"unique_ids":{c:len(v) for c,v in ids.items()},
            "trial_cells":len(frame[trials].dropna().drop_duplicates()) if trials else None}


def _plain_level(value):
    """Return a supported, typed JSON scalar for a factor level."""
    if isinstance(value, np.generic):
        value = value.item()
    if type(value) in {str, int, bool}:
        return value
    if type(value) is float and np.isfinite(value):
        return value
    raise InferenceBlocked(
        "selected_levels_value",
        f"Selected factor levels must be finite JSON strings, numbers or booleans; got {type(value).__name__}",
    )


def _level_missing(value):
    if value is None or value is pd.NA or value is pd.NaT:
        return True
    try:
        missing = pd.isna(value)
        if isinstance(missing, (bool, np.bool_)) and bool(missing):
            return True
    except (TypeError, ValueError):
        pass
    return isinstance(value, str) and not value.strip()


def _same_level(left, right):
    """Typed equality keeps False distinct from 0 and integers from floats."""
    try:
        left = _plain_level(left)
        right = _plain_level(right)
    except InferenceBlocked:
        return False
    return type(left) is type(right) and left == right


def _level_list(values):
    result = []
    for value in values:
        if _level_missing(value):
            continue
        try:
            plain = _plain_level(value)
        except InferenceBlocked:
            plain = value
        if not any(_same_level(plain, existing) for existing in result):
            result.append(plain)
    return result


def _level_mask(series, levels):
    return series.map(lambda value: any(_same_level(value, level) for level in levels))


def _level_codes(series, levels):
    return [next((index for index, level in enumerate(levels) if _same_level(value, level)), None)
            for value in series.tolist()]


def _has_typed_level_collision(levels):
    return any(
        type(left) is not type(right) and left == right
        for index, left in enumerate(levels) for right in levels[index + 1:]
    )


def _factor_categories(series, levels):
    """Keep ordinary labels intact but encode Python-equal typed labels distinctly."""
    collides = _has_typed_level_collision(levels)
    if collides:
        categories = [f"__typed_level_{index}__" for index in range(len(levels))]
        return [categories[code] if code is not None else None for code in _level_codes(series, levels)], categories
    return series.tolist(), levels


def _apply_selected_levels_impl(frame, design, audit):
    """Apply the documented subset option and retain a per-target selection audit."""
    raw = design.get("selected_levels")
    declarations = {
        column for column in (design.get("group_column"), design.get("condition_column"))
        if isinstance(column, str) and column
    }
    record = {
        "mode": "all" if raw is None else "subset",
        "applied": raw is not None,
        "requested": _json(raw),
        "rows_before": len(frame),
        "ids_before": _ids(frame, design),
        "actual_levels_before": {column: _json(_level_list(frame[column].tolist()))
                                 for column in declarations if column in frame},
        "columns": {},
        "exclusions": [],
    }
    audit["user_level_selection"] = record
    if raw is None:
        record.update(selection_status="all_levels", rows_after=len(frame), ids_after=_ids(frame, design), actual_levels_after={
            column: _json(_level_list(frame[column].tolist())) for column in declarations if column in frame
        })
        return frame
    if not isinstance(raw, dict):
        raise InferenceBlocked("selected_levels_format", "options.selected_levels must be a JSON object mapping declared factor columns to lists")
    if not raw:
        raise InferenceBlocked("selected_levels_empty", "options.selected_levels is an empty object; omit it for all levels or select explicit levels")
    unknown_columns = [column for column in raw if not isinstance(column, str) or column not in declarations]
    if unknown_columns:
        record["unknown_columns"] = _json(unknown_columns)
        record.update(rows_after=len(frame), ids_after=_ids(frame, design))
        raise InferenceBlocked(
            "selected_levels_unknown_column",
            "Selected-level columns must be declared group_column or condition_column; unknown: " + ", ".join(map(str, unknown_columns)),
        )

    normalized: dict[str, list[Any]] = {}
    for column, requested in raw.items():
        if not isinstance(requested, list):
            raise InferenceBlocked("selected_levels_format", f"Selected levels for {column} must be a list")
        if not requested:
            record["columns"][column] = {
                "requested_levels": [], "available_levels": _json(_level_list(frame[column].tolist())) if column in frame else [],
                "rows_before": len(frame), "rows_after": 0, "rows_excluded": len(frame),
                "reason": "explicit_empty_selection",
            }
            record.update(selection_status="blocked", rows_after=0, ids_after={}, actual_levels_after={})
            raise InferenceBlocked("selected_levels_empty", f"No levels are selected for {column}; enable the subset only after selecting at least one level")
        levels = []
        for value in requested:
            if _level_missing(value):
                raise InferenceBlocked("selected_levels_value", f"Missing/blank labels cannot be selected for {column}")
            level = _plain_level(value)
            if any(_same_level(level, existing) for existing in levels):
                raise InferenceBlocked("selected_levels_duplicate", f"Selected levels for {column} contain a duplicate typed value: {level!r}")
            levels.append(level)
        normalized[column] = levels

    selected = frame.copy()
    for column, requested in normalized.items():
        if column not in frame:
            record["columns"][column] = {
                "requested_levels": _json(requested), "available_levels": [],
                "rows_before": len(selected), "rows_after": len(selected),
                "error": "declared selection column is absent from this target",
            }
            record.update(rows_after=len(selected), ids_after=_ids(selected, design))
            raise InferenceBlocked("selected_levels_column_missing", f"Selected-level column {column} is not present in this hypothesis input")
        available = _level_list(frame[column].tolist())
        absent = [level for level in requested if not any(_same_level(level, item) for item in available)]
        if absent:
            record["columns"][column] = {
                "requested_levels": _json(requested), "available_levels": _json(available),
                "missing_requested_levels": _json(absent), "rows_before": len(selected),
                "rows_after": len(selected),
            }
            record.update(rows_after=len(selected), ids_after=_ids(selected, design))
            raise InferenceBlocked(
                "selected_levels_unknown_level",
                f"Requested level(s) {absent!r} for {column} are absent from this metric/stratum/performance target; available: {available!r}",
            )
        before_column = selected
        mask = _level_mask(selected[column], requested)
        excluded = selected.loc[~mask]
        exclusion_levels = []
        for level in _level_list(excluded[column].tolist()):
            level_mask = _level_mask(excluded[column], [level])
            rows = excluded.loc[level_mask]
            exclusion_levels.append({"level": _json(level), "n_rows": len(rows), "ids": _ids(rows, design)})
        missing_mask = selected[column].map(_level_missing)
        missing_rows = selected.loc[missing_mask]
        if len(missing_rows):
            exclusion_levels.append({"level": None, "reason": "missing_or_blank_level", "n_rows": len(missing_rows), "ids": _ids(missing_rows, design)})
        selected = selected.loc[mask].copy()
        record["columns"][column] = {
            "requested_levels": _json(requested), "available_levels": _json(available),
            "actual_levels_after_filter": _json(_level_list(selected[column].tolist())),
            "rows_before": len(before_column), "rows_after": len(selected),
            "ids_before": _ids(before_column, design), "ids_after": _ids(selected, design),
            "rows_excluded": len(excluded), "excluded_by_level": exclusion_levels,
        }
        record["exclusions"].append({"column": column, "n_rows": len(excluded), "by_level": exclusion_levels})
    record.update(
        selection_status="applied",
        rows_after=len(selected), ids_after=_ids(selected, design),
        actual_levels_after={column: _json(_level_list(selected[column].tolist()))
                             for column in declarations if column in selected},
    )
    return selected


def _apply_selected_levels(frame, design, audit):
    try:
        return _apply_selected_levels_impl(frame, design, audit)
    except InferenceBlocked:
        record = audit.get("user_level_selection")
        if isinstance(record, dict):
            record.setdefault("selection_status", "blocked")
            record.setdefault("rows_after", record.get("rows_before", len(frame)))
            record.setdefault("ids_after", record.get("ids_before", _ids(frame, design)))
            record.setdefault("actual_levels_after", {})
        raise


def _require_selected_levels(frame, design, stage):
    requested = design.get("selected_levels")
    if not isinstance(requested, dict):
        return
    for column, levels in requested.items():
        if column not in frame:
            raise InferenceBlocked("selected_levels_column_missing", f"Selected-level column {column} is absent after {stage}")
        observed = _level_list(frame[column].tolist())
        missing = [level for level in levels if not any(_same_level(level, value) for value in observed)]
        if missing:
            raise InferenceBlocked(
                "selected_level_no_observations",
                f"Selected level(s) {missing!r} for {column} have no complete observations after {stage}; the planned hypothesis remains blocked",
            )


def _prepare(frame,design,audit,columns):
    audit.update(input_rows=len(frame),input_ids=_ids(frame,design),input_counts=_counts(frame,design),exclusions=[])
    missing=[c for c in columns if c not in frame]
    if missing:
        raise InferenceBlocked("missing_design_columns","Required columns missing: "+", ".join(missing))
    invalid=~np.isfinite(pd.to_numeric(frame["value"],errors="coerce"))
    if "status" in frame:
        invalid |= ~frame["status"].astype(str).isin({"ok","complete","computed","available","warning","valid"})
    frame=_audit_exclude(audit,frame,invalid,"metric_missing_nonfinite_or_not_computed")
    missingids=frame[columns].isna().any(axis=1)
    for col in columns:
        if frame[col].dtype==object:
            missingids |= frame[col].astype(str).str.strip().eq("")
    frame=_audit_exclude(audit,frame,missingids,"required_design_id_missing")
    frame["value"]=pd.to_numeric(frame["value"])
    audit.update(retained_rows=len(frame),retained_ids=_ids(frame,design),retained_counts=_counts(frame,design))
    return frame


def _unit(design):
    unit=design.get("analysis_unit")
    column=design.get("analysis_unit_column")
    if unit in {"subject","individual","participant"}:
        return column or design.get("subject_column")
    if unit in {"team","interaction"}:
        return column or design.get("team_column") or design.get("interaction_column")
    raise InferenceBlocked("unknown_analysis_unit","Explicit subject or team analysis unit is required; pair rows are not independent units")


def _distinct_scope_values(frame,column):
    if column not in frame:return set(),False
    values=frame[column]
    def missing_value(value):
        if value is None:return True
        try:
            missing=pd.isna(value)
            return bool(missing) if isinstance(missing,(bool,np.bool_)) else False
        except (TypeError,ValueError):return False
    selected={str(value) for value in values if not missing_value(value) and str(value).strip()}
    missing=any(missing_value(value) or not str(value).strip() for value in values)
    return selected,missing


def _contains_overlap_dependency(frame):
    if "dependence_ref" not in frame:return False
    for value in frame["dependence_ref"].tolist():
        if value is None or (isinstance(value,float) and np.isnan(value)):continue
        refs=value if isinstance(value,(list,tuple,set,np.ndarray)) else (value,)
        if any("segment-overlap:" in str(ref).casefold() or "overlap-label:" in str(ref).casefold() for ref in refs if ref is not None):
            return True
    return False


def _scope_dependence_guard(frame,design):
    """Block pooled dependent labels/streams before repeated-model routing."""
    raw_strata=design.get("strata_columns",[])
    strata={raw_strata} if isinstance(raw_strata,str) else set(raw_strata or [])
    stream_ids,stream_missing=_distinct_scope_values(frame,"analysis_stream_id")
    if len(stream_ids)>1 or (stream_ids and stream_missing):
        raise InferenceBlocked("stream_pseudoreplication","Multiple or mixed analysis streams share this metric stratum; analyze one declared stream at a time")
    eye_labels,eye_missing=_distinct_scope_values(frame,"eye_label")
    if len(eye_labels)>1 or (eye_labels and eye_missing):
        raise InferenceBlocked("eye_stream_pseudoreplication","Multiple or mixed eye streams share this metric stratum; eye streams do not increase independent N")
    labels,label_missing=_distinct_scope_values(frame,"segment_id")
    if _contains_overlap_dependency(frame):
        if "segment_id" not in strata or len(labels)!=1 or label_missing:
            raise InferenceBlocked("overlap_label_pseudoreplication","Overlapping labels share original observations; declare one fixed segment_id stratum for inference")


def _structure(frame,design,unit):
    if not design.get("independence_confirmed",False):
        raise InferenceBlocked("independence_unconfirmed","Independent units and dependence structure must be explicitly confirmed")
    _scope_dependence_guard(frame,design)
    subject=design.get("subject_column");team=design.get("team_column") or design.get("interaction_column")
    group=design.get("group_column")
    if group in frame and unit in frame:
        changes=frame.groupby(unit,sort=False,dropna=False)[group].apply(lambda values:len(_level_list(values.tolist()))>1)
        if changes.any():
            raise InferenceBlocked("between_group_changes","An independent unit has multiple between-group assignments; repeated condition must be declared separately")
    # Detect actual sharing regardless of which analysis unit the user selected.
    if subject in frame and team in frame:
        membership=frame[[subject,team]].dropna()
        membership=membership.loc[membership[subject].astype(str).ne("") & membership[team].astype(str).ne("")]
        if not membership.empty and membership.groupby(subject)[team].nunique().max()>1:
            raise InferenceBlocked("cross_team_overlap","The same subject belongs to multiple teams; crossed membership is outside validated templates")
        if design.get("analysis_unit") in {"subject","individual","participant"} and not membership.empty:
            nested=design.get("design_type")=="lmm" and design.get("lmm_template")=="nested_team"
            if membership.groupby(team)[subject].nunique().max()>1 and not nested:
                raise InferenceBlocked("shared_team_members","Subjects share teams; use an explicit nested LMM or team aggregation")
    members=design.get("team_members",{})
    owners={}
    for group,identities in members.items():
        for identity in identities:
            if str(identity) in owners and owners[str(identity)]!=str(group):
                raise InferenceBlocked("cross_team_overlap","The member metadata reports a subject shared across teams")
            owners[str(identity)]=str(group)
    pairs=frame["pair_id"].notna() & frame["pair_id"].astype(str).ne("") if "pair_id" in frame else pd.Series(False,index=frame.index)
    if pairs.any():
        if design.get("analysis_unit") not in {"team","interaction"} or not design.get("aggregate_pairs",False):
            raise InferenceBlocked("pair_pseudoreplication","Pair results require declared team aggregation; pair count cannot become independent N")
        if not members and not design.get("pair_members"):
            raise InferenceBlocked("pair_membership_unknown","Pair/team member metadata is required to rule out shared members across teams")
        if design.get("pair_members"):
            owners={}
            for _,row in frame.loc[pairs].iterrows():
                pair=str(row["pair_id"])
                if pair not in design["pair_members"]:
                    raise InferenceBlocked("pair_membership_unknown","Missing member IDs for pair "+pair)
                for member in design["pair_members"][pair]:
                    owner=str(row[unit])
                    if str(member) in owners and owners[str(member)]!=owner:
                        raise InferenceBlocked("cross_team_overlap","Pair members occur in multiple teams")
                    owners[str(member)]=owner


def _levels(frame,column,design):
    actual=_level_list(frame[column].tolist())
    references=design.get("reference_levels",{})
    if column in references:
        ref=references[column]
        matching=next((value for value in actual if _same_level(ref,value)),None)
        if matching is None:
            raise InferenceBlocked("reference_level_absent",f"Reference {ref!r} absent for {column}")
        actual=[matching]+sorted([x for x in actual if not _same_level(x,matching)],key=str)
    else:
        actual=sorted(actual,key=str)
    return actual


def _describe(frame):
    values=pd.to_numeric(frame["value"],errors="coerce")
    values=values[np.isfinite(values)]
    return {"n_finite":len(values),"mean":float(values.mean()) if len(values) else None,
            "sd":float(values.std(ddof=1)) if len(values)>1 else None,
            "median":float(values.median()) if len(values) else None,
            "min":float(values.min()) if len(values) else None,"max":float(values.max()) if len(values) else None}


def _model(frame,design,unit,cancel=None):
    """Fixed finite templates, full-rank checks, no optimizer/model fishing."""
    import statsmodels.formula.api as smf
    _check_cancel(cancel)
    template=design.get("lmm_template","subject_intercept")
    if template not in {"subject_intercept","subject_condition_slope","nested_team","within_between"}:
        raise InferenceBlocked("lmm_template","Unsupported LMM template")
    subject=design.get("subject_column") if template=="nested_team" else unit
    group=design.get("group_column");condition=design.get("condition_column")
    covariates=list(design.get("covariates",[]))
    use_association=template=="within_between" or design.get("association_predictor") is not None
    required=["value",subject]+covariates
    use_group=group in frame and frame[group].notna().any()
    use_condition=condition in frame and frame[condition].notna().any()
    if use_group: required.append(group)
    if use_condition: required.append(condition)
    if template=="nested_team":
        team=design.get("team_column") or design.get("interaction_column")
        required.append(team)
    if use_association:
        predictor=design.get("association_predictor")
        if not predictor:
            raise InferenceBlocked("lmm_predictor","Within/between template requires an explicit continuous predictor")
        required.append(predictor)
    missing=[c for c in required if not c or c not in frame]
    if missing:
        raise InferenceBlocked("lmm_columns","Missing LMM model variables: "+str(missing))
    valid=frame[required].notna().all(axis=1)
    numeric=["value"]+covariates+([predictor] if use_association else [])
    for col in numeric:
        valid &= np.isfinite(pd.to_numeric(frame[col],errors="coerce"))
    excluded=frame.index[~valid].tolist();data=frame.loc[valid].copy()
    _require_selected_levels(data, design, "LMM model complete cases")
    _minimum(data[subject].nunique(),"subjects")
    if not (data.groupby(subject).size()>=2).all():
        raise InferenceBlocked("lmm_repetition","Random subject intercept requires repeated observations for each retained subject")
    modeldata=pd.DataFrame({"_y":data["value"].astype(float),"_subject":data[subject].astype(str)})
    factors={};centering={};fixed=[]
    for key,col,use in (("_group",group,use_group),("_condition",condition,use_condition)):
        if use:
            levels=_levels(data,col,design)
            if len(levels)<2:
                raise InferenceBlocked("factor_one_level",f"Factor {col} has only one retained level")
            category_values,category_levels=_factor_categories(data[col],levels)
            modeldata[key]=pd.Categorical(category_values,categories=category_levels,ordered=True)
            factors[col]={"levels":list(map(str,levels)),"reference":str(levels[0]),"coding":"Treatment"}
            if _has_typed_level_collision(levels):
                factors[col].update(typed_levels=_json(levels),model_category_mapping=_json(category_levels),
                                    typed_reference=_json(levels[0]))
            fixed.append(f"C({key}, Treatment)")
    formula="_y ~ "+(" * ".join(fixed) if fixed else "1")
    for i,col in enumerate(covariates):
        mean=float(pd.to_numeric(data[col]).mean());centering[col]=mean
        modeldata[f"_cov{i}"]=pd.to_numeric(data[col])-mean
        formula+=f" + _cov{i}"
    re_formula="1";vc=None;groups=modeldata["_subject"]
    n_independent=data[subject].nunique()
    if template=="subject_condition_slope":
        if not use_condition:
            raise InferenceBlocked("slope_condition","Random condition slope needs a declared repeated condition")
        condition_values,_=_factor_categories(data[condition],_levels(data,condition,design))
        counts=pd.crosstab(data[subject],pd.Series(condition_values,index=data.index))
        if (counts==0).any().any():
            raise InferenceBlocked("slope_coverage","Each subject must cover every random-slope condition")
        if (counts<2).any().any():
            raise InferenceBlocked("slope_unidentifiable","Unstructured categorical random slopes need replicated observations per subject/condition to separate residual covariance")
        re_formula="1 + C(_condition, Treatment)"
    elif template=="nested_team":
        modeldata["_team"]=data[team].astype(str)
        n_independent=data[team].nunique()
        if n_independent<10:
            raise InferenceBlocked("asymptotic_team_n","Asymptotic nested-team inference needs at least ten independent teams")
        groups=modeldata["_team"];vc={"subject":"0 + C(_subject)"}
    if use_association:
        means=data.groupby(subject)[predictor].transform("mean")
        modeldata["_within"]=pd.to_numeric(data[predictor])-means
        modeldata["_between"]=means-float(data.groupby(subject)[predictor].mean().mean())
        centering[predictor]="within subject mean; between centred on unweighted subject means"
        formula+=" + _within + _between"
        if modeldata["_within"].var()<=0 or modeldata["_between"].var()<=0:
            raise InferenceBlocked("within_between_variance","Within/between template needs variability at both levels")
    _check_cancel(cancel)
    method=design.get("fit_method","REML")
    if method not in {"REML","ML"}:
        raise InferenceBlocked("fit_method","Only ML or REML is supported")
    with warnings.catch_warnings(record=True) as captured:
        warnings.simplefilter("always")
        model=smf.mixedlm(formula,modeldata,groups=groups,re_formula=re_formula,vc_formula=vc)
        if np.linalg.matrix_rank(model.exog)<model.exog.shape[1]:
            raise InferenceBlocked("rank_deficient_fixed","Fixed-effects design is rank deficient")
        # Exact within-unit fits make the residual-variance MLE a boundary point;
        # do not accept an optimizer's tiny positive approximation as information.
        within_y=model.endog-pd.Series(model.endog,index=modeldata.index).groupby(modeldata["_subject"]).transform("mean").to_numpy()
        within_x=np.column_stack([model.exog[:,j]-pd.Series(model.exog[:,j],index=modeldata.index).groupby(modeldata["_subject"]).transform("mean").to_numpy() for j in range(model.exog.shape[1])])
        residual=within_y-within_x@np.linalg.lstsq(within_x,within_y,rcond=None)[0]
        if np.dot(residual,residual)<=np.finfo(float).eps*max(float(np.dot(within_y,within_y)),np.finfo(float).tiny)*64:
            raise InferenceBlocked("zero_residual_variance","The observed within-unit fixed-effect residual is exactly degenerate")
        result=model.fit(reml=method=="REML",method="lbfgs",maxiter=500,disp=False)
    _check_cancel(cancel)
    details={"template":template,"formula":formula,"column_mapping":{"_y":"value","_subject":subject,"_group":group,"_condition":condition},
             "random_formula":re_formula,"variance_components":vc,"fit_method":method,"optimizer":"lbfgs","maxiter":500,
             "backend":"statsmodels "+statsmodels.__version__,"contrast_coding":"Treatment","factors":factors,
             "centering":centering,"ci_method":"asymptotic normal Wald; no small-sample df correction",
             "converged":bool(result.converged),"warnings":[str(w.message) for w in captured],"excluded_model_rows":excluded,
             "retained_model_rows":data.index.tolist(),
             "residual_variance":float(result.scale),"random_covariance":np.asarray(result.cov_re).tolist(),
             "variance_component_estimates":np.asarray(result.vcomp).tolist(),"log_likelihood":float(result.llf),
             "n_independent":n_independent,"n_observations":len(data)}
    def model_block(code,message):
        error=InferenceBlocked(code,message)
        error.diagnostics=details
        raise error
    if not result.converged:
        model_block("lmm_nonconvergence","MixedLM did not converge: "+"; ".join(details["warnings"]))
    if not np.isfinite(result.scale) or result.scale<=np.finfo(float).eps*max(float(modeldata["_y"].var()),np.finfo(float).tiny)*64:
        model_block("zero_residual_variance","MixedLM residual variance is zero/degenerate; Wald inference is unavailable")
    eigen=np.linalg.eigvalsh(np.asarray(result.cov_re))
    scale=max(float(result.scale),float(eigen.max()) if eigen.size else 0,np.finfo(float).tiny)
    if eigen.size and (eigen.min()/scale<1e-8 or eigen.min()/max(eigen.max(),np.finfo(float).tiny)<1e-8):
        model_block("lmm_singular","Random-effects covariance is singular/on the boundary; no simpler model fallback")
    if len(result.vcomp) and np.min(result.vcomp)/scale<1e-8:
        model_block("lmm_singular","Nested variance component is singular/on the boundary")
    rows=[];ci=result.conf_int()
    for term,estimate in result.fe_params.items():
        se=float(result.bse[term]);z=float(estimate/se)
        if not np.isfinite([estimate,se,z]).all() or se<=0:
            model_block("lmm_invalid_covariance","Fixed-effect covariance cannot support inference")
        rows.append(dict(method="LMM",contrast=str(term),estimate=float(estimate),ci_low=float(ci.loc[term,0]),ci_high=float(ci.loc[term,1]),
                         ci_method=details["ci_method"],statistic=z,statistic_name="z",df=None,p=float(2*stats.norm.sf(abs(z))),
                         effect=float(estimate),effect_name="fixed coefficient",effect_definition="Unstandardized fixed coefficient under Treatment coding",
                         effect_ci_low=float(ci.loc[term,0]),effect_ci_high=float(ci.loc[term,1]),
                         n_independent=n_independent,n_observations=len(data),model=details))
    # Optional predeclared fixed-effect comparison: same complete cases, random structure, and ML.
    reduced=design.get("compare_fixed_terms")
    if reduced is not None:
        allowed={"group":"C(_group, Treatment)","condition":"C(_condition, Treatment)","interaction":"C(_group, Treatment):C(_condition, Treatment)"}
        if not isinstance(reduced,list) or any(t not in allowed for t in reduced):
            raise InferenceBlocked("lmm_comparison","Reduced fixed terms must be an explicit list of group/condition/interaction")
        reduced_formula="_y ~ "+(" + ".join(allowed[t] for t in reduced) if reduced else "1")
        for i in range(len(covariates)): reduced_formula+=f" + _cov{i}"
        if use_association: reduced_formula+=" + _within + _between"
        reduced_model=smf.mixedlm(reduced_formula,modeldata,groups=groups,re_formula=re_formula,vc_formula=vc)
        with warnings.catch_warnings(record=True) as comparison_warnings:
            warnings.simplefilter("always")
            full_ml=model.fit(reml=False,method="lbfgs",maxiter=500,disp=False)
            reduced_ml=reduced_model.fit(reml=False,method="lbfgs",maxiter=500,disp=False)
        difference=model.exog.shape[1]-reduced_model.exog.shape[1]
        if difference<=0 or np.linalg.matrix_rank(np.column_stack([model.exog,reduced_model.exog]))!=np.linalg.matrix_rank(model.exog):
            raise InferenceBlocked("lmm_not_nested","Reduced fixed effects must be a strict nested subspace")
        for fitted in (full_ml,reduced_ml):
            if not fitted.converged or not np.isfinite(fitted.llf) or np.linalg.eigvalsh(np.asarray(fitted.cov_re)).min()/max(fitted.scale,np.finfo(float).tiny)<1e-8 or (len(fitted.vcomp) and np.min(fitted.vcomp)/max(fitted.scale,np.finfo(float).tiny)<1e-8):
                raise InferenceBlocked("lmm_comparison_failed","ML comparison model did not converge or has boundary covariance")
        lr=float(2*(full_ml.llf-reduced_ml.llf))
        if lr < -1e-7:
            raise InferenceBlocked("lmm_likelihood_order","Full ML log likelihood is below reduced ML likelihood")
        lr=max(0,lr)
        rows.append(dict(method="LMM fixed-effects ML likelihood ratio",estimate=None,statistic=lr,statistic_name="chi2",df=difference,
                         p=float(stats.chi2.sf(lr,difference)),n_independent=n_independent,n_observations=len(data),
                         model={**details,"comparison_fit_method":"ML","reduced_formula":reduced_formula,"same_complete_cases":True,
                                "full_ml_llf":float(full_ml.llf),"reduced_ml_llf":float(reduced_ml.llf),
                                "comparison_warnings":[str(w.message) for w in comparison_warnings]}))
    return rows,details


def _performance_join(frame,performance,design,audit):
    columns=list(design.get("performance_columns",[]));keys=list(design.get("performance_join_keys",[]))
    if performance is None or performance.empty:
        raise InferenceBlocked("performance_missing","No performance table was supplied")
    if not keys or any(c not in frame or c not in performance for c in keys):
        raise InferenceBlocked("performance_keys","Explicit complete performance join keys are required")
    if any(c not in performance for c in columns):
        raise InferenceBlocked("performance_columns","Selected performance columns are missing")
    if frame[keys].isna().any().any() or performance[keys].isna().any().any():
        raise InferenceBlocked("performance_null_keys","Null join keys are not permitted")
    if frame.duplicated(keys).any() or performance.duplicated(keys).any():
        raise InferenceBlocked("performance_join_multiplicity","Performance join must be one-to-one for each metric/stratum; duplicate keys blocked")
    overlap=set(columns)&set(frame.columns)
    if overlap:
        raise InferenceBlocked("performance_name_collision","Performance column collides with metric fields: "+str(sorted(overlap)))
    audit["performance_join"]={"keys":keys,"validation":"one_to_one","performance_input_rows":len(performance)}
    return frame.merge(performance[keys+columns],on=keys,how="left",validate="one_to_one",sort=False)


def _model_audit(audit,frame,design,details):
    retained=_audit_exclude(audit,frame,frame.index.isin(details["excluded_model_rows"]),"model_required_variable_missing_nonfinite")
    audit.update(model=details,retained_rows=len(retained),retained_ids=_ids(retained,design),retained_counts=_counts(retained,design))


def _route(frame,design,audit,cancel):
    dtype=design.get("design_type","unknown")
    if dtype=="unknown":
        raise InferenceBlocked("unknown_design","Study design is unknown; supply the missing design information")
    if design.get("outcome_type","unknown")!="continuous":
        raise InferenceBlocked("unsupported_outcome","Only explicitly continuous outcomes are supported; binary, count, bounded, censored or unknown outcomes require validated specialized models")
    unit=_unit(design); group=design.get("group_column");condition=design.get("condition_column")
    columns=[unit]
    if dtype in {"independent","team_independent"}: columns.append(group)
    if dtype in {"paired","repeated"}: columns.append(condition)
    if not all(columns):
        raise InferenceBlocked("design_id_missing","Required group/condition/unit column is not declared")
    if any(c not in frame for c in columns):
        raise InferenceBlocked("missing_design_columns","Required columns missing: "+", ".join(c for c in columns if c not in frame))
    _structure(frame,design,unit)
    frame=_apply_selected_levels(frame,design,audit)
    frame=_prepare(frame,design,audit,columns)
    _require_selected_levels(frame,design,"complete metric observations")
    if frame.empty:
        raise InferenceBlocked("no_complete_observations","No complete finite observations with the required design IDs remain")
    # Pair/member aggregation is deliberate and changes estimand to team mean.
    if design.get("aggregate_pairs",False):
        by=[unit]+([group] if dtype in {"independent","team_independent"} else [condition] if dtype in {"paired","repeated"} else [])
        if dtype not in {"independent","team_independent","paired","repeated"}:
            raise InferenceBlocked("aggregation_template","Explicit pair aggregation supports group comparisons or paired/repeated team conditions")
        audit["aggregation"]={"operation":"unweighted mean within team × declared factor","keys":by,"input_rows":len(frame)}
        frame=frame.groupby(by,as_index=False,dropna=False)["value"].mean()
        declared=design.get("team_members") or design.get("pair_members") or {}
        audit["aggregation"].update(output_rows=len(frame),n_independent_teams=frame[unit].nunique(),
                                   n_declared_members=len({str(person) for people in declared.values() for person in people}))
    ci_method=design.get("bootstrap_method","percentile")
    if dtype in {"independent","team_independent"}:
        if frame.duplicated(unit).any():
            raise InferenceBlocked("repeated_independent_unit","Independent design requires exactly one observation per independent unit; use repeated/LMM or explicit aggregation")
        levels=_levels(frame,group,design)
        groups=[frame.loc[_level_mask(frame[group], [v]),"value"].to_numpy(float) for v in levels]
        audit["levels"]=_json(levels)
        if len(levels)==2:
            result=welch_t(*groups,cancel=cancel,ci_method=ci_method);result["contrast"]=_json(levels)
            return [result]
        if len(levels)>=3:
            return [welch_anova(groups)]+games_howell(groups,levels)
        raise InferenceBlocked("levels","At least two experimental groups are required")
    if dtype in {"paired","repeated"}:
        if group in frame and len(_level_list(frame[group].tolist()))>1:
            rows,details=_model(frame,{**design,"lmm_template":"subject_intercept"},unit,cancel)
            _model_audit(audit,frame,design,details); audit["route_reason"]="Between × within factors require LMM; no mixed ANOVA claim"
            return rows
        levels=_levels(frame,condition,design)
        paired=frame.copy()
        paired["_condition_level_index"]=_level_codes(paired[condition],levels)
        if paired.duplicated([unit,"_condition_level_index"]).any():
            if dtype=="repeated":
                rows,details=_model(frame,{**design,"lmm_template":"subject_intercept"},unit,cancel)
                _model_audit(audit,frame,design,details); audit["route_reason"]="Repeated trials require LMM; no averaging guessed"
                return rows
            raise InferenceBlocked("duplicate_pair_cells","More than one observation per unit/condition; pairing cannot be guessed")
        wide=paired.pivot(index=unit,columns="_condition_level_index",values="value").reindex(columns=range(len(levels)))
        complete=wide.notna().all(axis=1)
        incomplete=wide.index[~complete].tolist()
        if dtype=="repeated" and len(levels)>=3 and incomplete:
            rows,details=_model(frame,{**design,"lmm_template":"subject_intercept"},unit,cancel)
            _model_audit(audit,frame,design,details);audit["route_reason"]="Unbalanced/incomplete repeated design routed to fixed LMM template"
            return rows
        if incomplete:
            audit["exclusions"].append({"reason":"incomplete_pair","unit_ids":_json(incomplete),"n_units":len(incomplete)})
        wide=wide.loc[complete]
        retained=frame.loc[frame[unit].isin(wide.index)]
        audit.update(retained_rows=int(wide.size),retained_ids=_ids(retained,design),retained_counts=_counts(retained,design),retained_unit_ids=wide.index.astype(str).tolist(),levels=_json(levels))
        if len(levels)==2:
            result=paired_t(wide.iloc[:,0],wide.iloc[:,1],cancel=cancel,ci_method=ci_method);result["contrast"]=_json(levels)
            return [result]
        if dtype=="repeated" and len(levels)>=3:
            return [repeated_anova(wide.to_numpy(float),cancel=cancel)]
        raise InferenceBlocked("paired_levels","Paired design needs two conditions; >=3 levels require repeated design")
    if dtype=="lmm":
        rows,details=_model(frame,design,unit,cancel);_model_audit(audit,frame,design,details)
        return rows
    raise InferenceBlocked("unsupported_design","Unsupported design template: "+str(dtype))


def run_statistics(metrics,design,performance=None,cancel=None,progress=None)->AnalysisBundle:
    """Run supported inference over metric × explicit stratum and performance.

    ``design`` is DesignSpec or a JSON dict with its fields plus documented fixed
    template options. No incomplete design is reconstructed from IDs or row order.
    """
    validate_metric_table(metrics)
    design=asdict(design) if isinstance(design,DesignSpec) else dict(design)
    options=design.get("options",{})
    if not isinstance(options,dict):
        raise InferenceBlocked("design_options","Design options must be a JSON object")
    design={**DesignSpec().to_dict(),**options,**design}
    if "correction" in design and "family_correction" not in design:
        design["family_correction"]=design["correction"]
    if design.get("random_slope",False) and "lmm_template" not in design:
        design["lmm_template"]="subject_condition_slope"
    locked={"seed":SEED,"resamples":BOOTSTRAPS,"bootstrap_resamples":BOOTSTRAPS,"alpha":.05,"confidence_level":.95}
    unsupported_parameters=[k for k,v in locked.items() if k in design and design[k]!=v]
    requested_correction=design.get("family_correction","Holm" if design.get("confirmatory",False) else "BY")
    rows=[];desc=[];diagnostics=[];audits=[];logs=[];cancelled=False
    strata=list(design.get("strata_columns",[]))
    missing=[c for c in strata if c not in metrics]
    if missing:
        raise InferenceBlocked("strata_columns","Missing explicit strata: "+str(missing))
    grouping=["metric_id"]+strata
    frame=metrics.copy();frame["_source_row"]=np.arange(len(frame))
    groups=list(frame.groupby(grouping[0] if len(grouping)==1 else grouping,sort=False,dropna=False))
    for index,(keys,part) in enumerate(groups):
        keys=(keys,) if len(grouping)==1 else keys
        context=dict(zip(grouping,map(_json,keys)))
        metric_design={**design}
        types=design.get("metric_types",{})
        if str(context["metric_id"]) in types: metric_design["outcome_type"]=types[str(context["metric_id"])]
        units=part.loc[part["value"].notna(),"unit"].dropna().astype(str).unique()
        context.update(unit=units[0] if len(units)==1 else None,input_unit=units[0] if len(units)==1 else None,algorithm_version=METHOD_VERSION)
        descriptive_audit={}
        try:
            descriptive_part=_apply_selected_levels(part,metric_design,descriptive_audit)
            descriptive_values=_describe(descriptive_part)
            desc.append({**context,**descriptive_values,
                         "population":"selected_levels" if metric_design.get("selected_levels") is not None else "all_levels",
                         "selection_mode":descriptive_audit["user_level_selection"]["mode"],
                         "requested_levels":_json(metric_design.get("selected_levels")),
                         "actual_levels":descriptive_audit["user_level_selection"].get("actual_levels_after",{}),
                         "n_input_rows":len(part),"n_selected_rows":len(descriptive_part),
                         "status":"complete" if descriptive_values["n_finite"] else "no_finite_selected_values"})
        except InferenceBlocked as exc:
            selection_record=descriptive_audit.get("user_level_selection",{})
            desc.append({**context,"n_finite":0,"mean":None,"sd":None,"median":None,"min":None,"max":None,
                         "population":"selected_levels" if metric_design.get("selected_levels") is not None else "all_levels",
                         "selection_mode":selection_record.get("mode"),
                         "requested_levels":_json(metric_design.get("selected_levels")),
                         "actual_levels":selection_record.get("actual_levels_after",{}),
                         "n_input_rows":len(part),"n_selected_rows":selection_record.get("rows_after",0),
                         "status":"blocked","reason_code":exc.code,"reason":str(exc)})
        targets=list(design.get("performance_columns",[])) or [None]
        for target in targets:
            audit={"hypothesis_id":f"H{len(audits)+1:05d}","context":context,"outcome":target,
                   "design":metric_design,"seed":SEED,"resamples":BOOTSTRAPS,"comparison_family":design.get("comparison_family","all_selected_metrics_strata_performance"),
                   "input_rows":len(part),"input_ids":_ids(part,metric_design),"exclusions":[]}
            base={**context,"hypothesis_id":audit["hypothesis_id"],"outcome":target or "metric value",
                  "analysis_unit":design.get("analysis_unit"),"family_id":audit["comparison_family"],"status":"complete","reason_code":None,
                  "n_input":len(part),"alpha":.05,"seed":SEED,"bootstrap_resamples":BOOTSTRAPS}
            try:
                _check_cancel(cancel)
                if unsupported_parameters:
                    raise InferenceBlocked("unvalidated_statistics_parameters","Validated defaults are locked for this method version; unsupported overrides: "+", ".join(unsupported_parameters))
                if requested_correction not in {"BY","BH","Holm"}:
                    raise InferenceBlocked("unknown_correction","Correction must be BY, BH or Holm")
                if len(units)!=1:
                    raise InferenceBlocked("inconsistent_units","Each metric/stratum must have exactly one explicit unit")
                if target is None:
                    results=_route(part,metric_design,audit,cancel)
                else:
                    if design.get("design_type")=="unknown" or metric_design.get("outcome_type")!="continuous":
                        raise InferenceBlocked("association_design_unknown","Feature type and association design must be explicit and continuous")
                    if design.get("performance_types",{}).get(target,"unknown") not in {"continuous","ordinal"}:
                        raise InferenceBlocked("performance_type_unsupported","Performance type is unknown/binary/count/bounded/censored; no forced correlation")
                    unit=_unit(metric_design)
                    _structure(part,metric_design,unit)
                    selected_part=_apply_selected_levels(part,metric_design,audit)
                    clean=_prepare(selected_part,metric_design,audit,[unit])
                    clean=_performance_join(clean,performance,metric_design,audit)
                    clean[target]=pd.to_numeric(clean[target],errors="coerce")
                    clean=_audit_exclude(audit,clean,~np.isfinite(clean[target]),"performance_missing_nonfinite")
                    audit.update(retained_rows=len(clean),retained_ids=_ids(clean,metric_design),retained_counts=_counts(clean,metric_design))
                    _require_selected_levels(clean,metric_design,"complete metric/performance observations")
                    method=design.get("association_method","pearson")
                    if design["performance_types"][target]=="ordinal" and method!="spearman":
                        raise InferenceBlocked("ordinal_target","Ordinal performance requires the declared Spearman monotonic target")
                    repeat=clean.duplicated(unit).any()
                    if not repeat and design.get("design_type") in {"within_correlation","paired","repeated","lmm"}:
                        raise InferenceBlocked("no_repeated_observations","The declared within/repeated/model target has no repeated complete observations; it cannot become a between-unit ordinary correlation")
                    if repeat:
                        if design.get("design_type") not in {"within_correlation","repeated","lmm"}:
                            raise InferenceBlocked("correlation_pseudoreplication","Repeated units cannot be pooled as ordinary Pearson/Spearman rows")
                        if method!="pearson":
                            raise InferenceBlocked("repeated_spearman_unsupported","Repeated Spearman is outside validated templates")
                        if design.get("design_type")=="lmm":
                            if design.get("association_predictor") not in {None,target}:
                                raise InferenceBlocked("association_predictor_mismatch","Selected performance column differs from the declared model predictor")
                            model_design={**metric_design,"association_predictor":target}
                            results,details=_model(clean,model_design,unit,cancel)
                            _model_audit(audit,clean,metric_design,details)
                            audit.update(outcome="metric value",performance_column=target)
                            for result in results: result.update(outcome="metric value",performance_column=target)
                        else:
                            informative=[identity for identity,piece in clean.groupby(unit,sort=False)
                                         if len(piece)>=2 and piece["value"].var(ddof=1)>0 and piece[target].var(ddof=1)>0]
                            clean=_audit_exclude(audit,clean,~clean[unit].isin(informative),"no_within_subject_variation")
                            audit.update(retained_rows=len(clean),retained_ids=_ids(clean,metric_design),retained_counts=_counts(clean,metric_design))
                            result=within_association(clean,"value",target,unit,cancel)
                            result.pop("excluded_no_within_variation")
                            results=[result]
                    else:
                        results=[association(clean["value"],clean[target],method,cancel,design.get("bootstrap_method","percentile"))]
                for result in results:
                    if result.get("preadjusted",False):
                        result["family_id"]=str(base["family_id"])+"/GamesHowell/"+audit["hypothesis_id"]
                        result["family_test_count"]=sum(r.get("preadjusted",False) for r in results)
                        audit["games_howell_family"]={"id":result["family_id"],"count":result["family_test_count"],
                                                     "scope":"all pairs of this metric/stratum's condition groups; no cross-metric FWER claim"}
                    dimensionless=result.get("method") in {"Pearson","Spearman","Repeated-measures Pearson (rmcorr)","RM ANOVA (Greenhouse–Geisser)","Welch ANOVA"}
                    result["unit"]="1" if dimensionless else context["input_unit"]
                    result["statistic_unit"]="1"
                    result["effect_unit"]="1" if result.get("effect_name") in {"Hedges g","dz","partial eta squared","r","rho","within-subject r"} else result["unit"]
                    if result.get("method")=="LMM":
                        term=str(result.get("contrast",""))
                        if term.startswith("_cov"):
                            col=design.get("covariates",[])[int(term[4:])]
                            result["unit"]=str(context["input_unit"])+"/"+str(design.get("covariate_units",{}).get(col,"unknown predictor unit"))
                            result["effect_unit"]=result["unit"]
                        elif term in {"_within","_between"}:
                            predictorunit=design.get("performance_units",{}).get(target,design.get("predictor_unit","unknown predictor unit"))
                            result["unit"]=str(context["input_unit"])+"/"+str(predictorunit)
                            result["effect_unit"]=result["unit"]
                    n=result.get("n_independent",0)
                    if "aggregation" in audit:
                        result["n_people"]=audit["aggregation"]["n_declared_members"]
                        result["n_pair_observations"]=audit["aggregation"]["input_rows"]
                    result["warnings"]=(["Small number of independent units (3–19); inference assumptions and uncertainty require caution"] if n<20 else [])
                    if result.get("bootstrap",{}).get("status")=="failed":
                        result["warnings"].append("Bootstrap CI unavailable: "+result["bootstrap"]["reason"])
                    rows.append({**base,**result})
                audit["status"]="complete"
                audit["result_indices"]=list(range(len(rows)-len(results),len(rows)))
            except AnalysisCancelled:
                cancelled=True;rows.append({**base,"status":"cancelled","reason_code":"cancelled","reason":"Cancelled before this hypothesis completed"})
                audit["status"]="cancelled";audits.append(audit)
                break
            except InferenceBlocked as exc:
                detail=getattr(exc,"diagnostics",None)
                rows.append({**base,"status":"blocked","reason_code":exc.code,"reason":str(exc),"model":detail})
                audit.update(status="blocked",reason_code=exc.code,reason=str(exc))
                if detail is not None: audit["model"]=detail
                logs.append(audit["hypothesis_id"]+": "+exc.code+" — "+str(exc))
            except (ValueError,TypeError,KeyError,np.linalg.LinAlgError,FloatingPointError,ZeroDivisionError) as exc:
                rows.append({**base,"status":"failed","reason_code":"statistical_backend_failure","reason":type(exc).__name__+": "+str(exc)})
                audit.update(status="failed",reason=str(exc))
                logs.append(audit["hypothesis_id"]+": statistical backend failed: "+str(exc))
            audits.append(audit)
        if cancelled: break
        if progress: progress((index+1)/max(1,len(groups)),"统计 "+str(context["metric_id"]))
    # Families fixed before observing p-values; GH already controls its k-group family.
    correction=requested_correction
    if correction=="BH" and not design.get("bh_dependence_justified",False):
        correction="BY";logs.append("BH dependence conditions not confirmed; declared conservative BY policy applied")
    families={}
    for i,row in enumerate(rows):
        if row.get("p") is not None and not row.get("preadjusted",False) and row["status"]=="complete":
            families.setdefault(str(row["family_id"]),[]).append(i)
    for family,indices in families.items():
        adjusted=adjust_pvalues([rows[i]["p"] for i in indices],correction)
        for i,padj in zip(indices,adjusted):
            rows[i]["p_adj"]=float(padj);rows[i]["correction"]=correction;rows[i]["family_test_count"]=len(indices)
    for audit in audits:
        for index in audit.get("result_indices",[]):
            diagnostics.append({"hypothesis_id":audit["hypothesis_id"],"result_index":index,
                                "method":rows[index].get("method"),"warnings":rows[index].get("warnings",[]),
                                "bootstrap":rows[index].get("bootstrap"),"model":rows[index].get("model")})
    statuses={r["status"] for r in rows}
    status="cancelled" if cancelled else "partial" if statuses&{"failed","blocked"} else "complete"
    if rows and statuses <= {"failed","blocked"}: status="failed"
    if not rows:
        status="failed";logs.append("empty_metrics: No metric observations were supplied; no inference was executed")
    family_summary={f:{"count":len(i),"correction":correction,"scope":"raw omnibus, association or model coefficient tests"} for f,i in families.items()}
    for audit in audits:
        if "games_howell_family" in audit:
            family=audit["games_howell_family"]
            family_summary[family["id"]]={"count":family["count"],"correction":"Games–Howell (already simultaneous)","scope":family["scope"]}
    metadata={"contract_version":CONTRACT_VERSION,"algorithm_version":METHOD_VERSION,"execution_status":status,
              "audit":{"design":design,"hypotheses":audits,"families":family_summary,
                       "resampling":{"seed":SEED,"resamples":BOOTSTRAPS,"confidence_level":.95},
                       "backend_versions":{"numpy":np.__version__,"scipy":scipy.__version__,"statsmodels":statsmodels.__version__}},
              "result_count":len(rows),"completed_hypotheses":sum(a.get("status")=="complete" for a in audits)}
    return AnalysisBundle(tables={"statistics":pd.DataFrame(_json(rows)),"descriptives":pd.DataFrame(_json(desc)),
                                  "diagnostics":pd.DataFrame(_json(diagnostics))},metadata=_json(metadata),logs=logs)
