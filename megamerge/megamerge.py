import json
import multiprocessing
import os
import tempfile
import time
from fontTools.ttLib import TTFont
from fontTools.merge import Merger, Options
from fontTools.ttLib.scaleUpem import scale_upem
from fontTools.ttLib.tables.otBase import USE_HARFBUZZ_REPACKER
from gftools.fix import rename_font

tiers = json.load(open('../fontrepos.json'))
state = json.load(open('../state.json'))
warnings = []

UPEM = 1000
_tmpdir = tempfile.mkdtemp(prefix="megamerge-upem-")


def normalize_upem(path):
    """Rescale a font to the common UPEM so fontTools' merger can combine it.

    fontTools requires head.unitsPerEm to be identical across all merged fonts
    and raises AssertionError otherwise. Returns a path to a normalized copy,
    or the original path when no rescaling is needed.
    """
    font = TTFont(path)
    if font['head'].unitsPerEm == UPEM:
        return path
    warnings.append(
        f"Rescaled {os.path.basename(path)} from "
        f"{font['head'].unitsPerEm} to {UPEM} upem before merging"
    )
    scale_upem(font, UPEM)
    out = os.path.join(_tmpdir, os.path.basename(path))
    font.save(out)
    return out


# Budget for the harfbuzz-assisted save before falling back, in seconds.
HB_REPACK_BUDGET = int(os.environ.get("MEGAMERGE_HB_BUDGET", "900"))


def _save_worker(font, path, use_hb, queue):
    try:
        if not use_hb:
            font.cfg[USE_HARFBUZZ_REPACKER] = False
        font.save(path)
        queue.put(None)
    except BaseException as exc:  # pragma: no cover - reported to the parent
        queue.put(f"{type(exc).__name__}: {exc}")


def save_font(font, path, newname):
    """Save a merged font, guarding against the GSUB repacker livelock.

    When a merged GSUB table overflows its 16-bit offsets, fontTools tries to
    resolve it by promoting lookups to Extension type. For lookup type 5
    (contextual substitution) it has no subtable splitter, so
    fixLookupOverFlows keeps reporting progress without ever shrinking the
    table and BaseTTXConverter.compile spins in its `while True` loop forever,
    logging "Don't know how to split GSUB lookup type 5" until the CI job hits
    the 6 hour limit.

    Harfbuzz packing is worth keeping when it works, so try it with a time
    budget and fall back to the pure-fontTools packer, which terminates.
    """
    for use_hb, budget, label in (
        (True, HB_REPACK_BUDGET, "harfbuzz"),
        (False, None, "fontTools-only"),
    ):
        queue = multiprocessing.Queue()
        proc = multiprocessing.Process(
            target=_save_worker, args=(font, path, use_hb, queue)
        )
        start = time.monotonic()
        proc.start()
        proc.join(budget)

        if proc.is_alive():
            proc.terminate()
            proc.join()
            warnings.append(
                f"{newname}: harfbuzz packing did not finish within "
                f"{budget}s (GSUB repacker livelock), retried without it"
            )
            continue

        error = queue.get() if not queue.empty() else None
        if proc.exitcode == 0 and error is None:
            print(f"  saved {os.path.basename(path)} "
                  f"using {label} packing in {time.monotonic() - start:.1f}s")
            return
        if use_hb:
            warnings.append(
                f"{newname}: harfbuzz packing failed ({error or proc.exitcode}), "
                f"retried without it"
            )
            continue
        raise RuntimeError(f"Failed to save {path}: {error or proc.exitcode}")


def megamerge(newname, base_font, tier_predicate, banned, modulation):
    glyph_count = len(TTFont(base_font).getGlyphOrder())
    selected_repos = [k for k, v in tiers.items() if tier_predicate(v.get("tier", 4))]
    selected_repos = [k for k in selected_repos if k not in banned]
    selected_repos = sorted(selected_repos, key=lambda k: tiers[k]["tier"])
    mergelist = [base_font]
    for repo in selected_repos:
        if "families" not in state[repo]:
            print(f"Skipping odd repo {repo} (no families)")
            continue
        selected_families = [x for x in state[repo]["families"].keys() if modulation in x and "UI" not in x]
        if not selected_families:
            continue
        files = state[repo]["families"][selected_families[0]]["files"]
        files = [ x for x in files if "Regular.ttf" in x and "UI" not in x]
        target = None
        for file in files:
            if "/hinted/" in file:
                target = file
                break
        if target is None:
            for file in files:
                if "/unhinted/" in file:
                    target = file
                    break
        if target is None:
            print(f"Couldn't find a target for {repo}")
            continue
        target_font = TTFont("../"+target)
        glyph_count += len(target_font.getGlyphOrder())
        if glyph_count > 65535:
            warnings.append(f"Too many glyphs while building {newname}, stopped at {repo}")
            break
        mergelist.append("../"+target)
    print("Merging: ")
    for x in mergelist:
        print("  "+os.path.basename(x))
    mergelist = [normalize_upem(x) for x in mergelist]
    merger = Merger(options=Options(drop_tables=["vmtx", "vhea", "MATH"]))
    merged = merger.merge(mergelist)
    rename_font(merged, newname)
    save_font(merged, newname.replace(" ","")+"-Regular.ttf", newname)


for modulation in ["Sans", "Serif"]:
    banned = ["duployan", "latin-greek-cyrillic", "sign-writing", "test"]
    if modulation == "sans":
        banned.append("devanagari")  # Already included
    megamerge(f"Noto {modulation} Living", 
            base_font=f"../fonts/Noto{modulation}/googlefonts/ttf/Noto{modulation}-Regular.ttf",
            tier_predicate= lambda x: x <= 3,
            banned=banned,
            modulation=modulation,
            )
    megamerge(f"Noto {modulation} Historical", 
            base_font=f"../fonts/Noto{modulation}/googlefonts/ttf/Noto{modulation}-Regular.ttf",
            tier_predicate= lambda x: x > 3,
            banned=banned,
            modulation=modulation
            )

if warnings:
    print("\n\nWARNINGS:")
    for w in warnings:
        print(w)
else:
    print("Completed successfully")