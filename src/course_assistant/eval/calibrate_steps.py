"""The calibration steps on a finished run, for `course-assistant calibrate` and
`scripts/calibrate.py` alike (E2.3 CAL-01, R3.1).

    export   the blind sheets: groundedness (EvalKit's sheet; `n` answers, half from tuning
             cases, half from held-out cases, `n_relabel` to relabel) and integrity
             (calibration/integrity/sheet.csv and relabel.csv)
    import   filled sheets: groundedness first round or relabel, integrity first round or relabel
    score    EvalKit's groundedness score and the integrity report, each guarded by the tuning
             log and logged to it as `calibration_scored` (naming the run folder and labeller)

Every guard lives in `course_assistant.eval.calibrate` and is kept: a sheet with labels is
never re-exported, a held-out case already scored under another rubric refuses, a held-out set
is scored once per rubric, a case listed as held-out but marked tuning refuses, and only a
logged scoring certifies a run (`certified_calibration`). Counts, ids and numbers only.
"""

from __future__ import annotations

from collections.abc import Set as AbstractSet
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# OWNER D-76, D-79: the model panel that labels when the owner does not
MODEL_PANEL = "model-panel:gpt-6.1-terra+gpt-6-luna;tiebreak=gpt-6.1-terra@high"
LABELLERS = ("owner", "model-panel")


def labeller_name(choice: str) -> str:
    if choice not in LABELLERS:
        raise ValueError(f"labeller must be one of {', '.join(LABELLERS)}")
    return "owner" if choice == "owner" else MODEL_PANEL


@dataclass(frozen=True)
class Exported:
    groundedness_csv: Path
    groundedness_rows: int
    integrity_csv: Path
    integrity_rows: int


def export(run_dir: Path, *, heldout_ids: AbstractSet[str], force: bool = False, n: int = 100,
           n_relabel: int = 20) -> Exported:  # fmt: skip
    from course_assistant.eval.calibrate import export_groundedness, export_probe, probe_dir

    _, csv_path, rows = export_groundedness(run_dir, force=force, heldout_ids=heldout_ids, n=n,
                                            n_relabel=n_relabel)  # fmt: skip
    probes = export_probe(run_dir, force=force, heldout_ids=heldout_ids, n=n,
                          n_relabel=n_relabel)  # fmt: skip
    return Exported(csv_path, rows, probe_dir(run_dir) / "sheet.csv", probes)


def import_groundedness(run_dir: Path, csv_path: Path, *, relabel: bool = False) -> int:
    """A filled groundedness sheet (S or U per claim). The first round goes through EvalKit's
    own import; the relabel round keeps only the sheet's relabel rows. Neither overwrites."""
    from evalkit import run as R
    from evalkit.pipeline.calibrate import LabelFile, labels_from_csv
    from evalkit.pipeline.labeller import load_labels, save_labels

    from course_assistant.eval.calibrate import CalibrationError

    if not relabel:
        return R.cal_import(run_dir, csv_path)
    rows = R._load_sheet(run_dir)  # pyright: ignore[reportPrivateUsage]
    target = run_dir / "calibration" / "labels-relabel.json"
    if load_labels(target) is not None:
        raise CalibrationError(f"{target.name} already exists; a relabel round is imported once")
    try:
        filled = labels_from_csv(csv_path.read_text(encoding="utf-8"), rows)
    except (OSError, ValueError) as exc:
        raise CalibrationError(f"cannot import {csv_path.name}: {exc}") from None
    wanted = {r.key for r in rows if r.relabel}
    labels = {k: v for k, v in filled.labels.items() if k in wanted}
    save_labels(target, LabelFile(sheet_id=filled.sheet_id, round="relabel", labels=labels))
    return len(labels)


def import_integrity(run_dir: Path, csv_path: Path, *, relabel: bool = False) -> int:
    from course_assistant.eval.calibrate import import_probe

    return import_probe(run_dir, csv_path, relabel=relabel)


def score(run_dir: Path, *, log: Path, heldout_ids: AbstractSet[str],
          labeller: str) -> tuple[Any, Any]:  # fmt: skip
    """(groundedness report, integrity report), each guarded and logged to `log`."""
    from course_assistant.eval.calibrate import score_groundedness, score_probe

    report = score_groundedness(run_dir, log=log, heldout_ids=heldout_ids, labeller=labeller)
    probe = score_probe(run_dir, log=log, heldout_ids=heldout_ids, labeller=labeller)
    return report, probe


def score_lines(report: Any, probe: Any) -> list[str]:
    out = [f"groundedness {a.judge}: held-out kappa {a.kappa.point:.2f} "
           f"[{a.kappa.lo:.2f}, {a.kappa.hi:.2f}] over {a.n_claims} claims"
           for a in report.agreements]  # fmt: skip
    out += [f"integrity {b.judge}: held-out kappa {b.kappa.point:.2f} "
            f"[{b.kappa.lo:.2f}, {b.kappa.hi:.2f}] over {b.n} conversations"
            for b in probe.agreements]  # fmt: skip
    return out
