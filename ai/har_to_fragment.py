#!/usr/bin/env python3
"""Record -> script: turn a browser HAR (DevTools > Network > Save all as HAR) into a framework fragment with
AUTOMATIC CORRELATION and PARAMETERISATION hints.

  python3 ai/har_to_fragment.py recording.har --name wishlist [--ai-review]

Deterministic correlation detection: a value found in an earlier JSON/HTML response that re-appears in a later request
(URL, query, body) is turned into an extractor (JSON Extractor / Regex Extractor) and `${var}` in the later request.
Auth calls are skipped (handled by `FRG - Authenticate`), static assets and other hosts are ignored.
--ai-review lets an LLM rename variables / drop false positives; it only sees variable names and request shapes, never values."""
import argparse, base64, json, os, re, sys
from urllib.parse import urlparse
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from jmx_min import code_ok, http, json_ex, regex_ex, tc, think_timer, write_fragment_file

STATIC = re.compile(r"\.(js|css|png|jpe?g|gif|svg|ico|woff2?|ttf|map|webp)(\?|$)", re.I)
AUTH = re.compile(r"/(oauth|auth|login|logout|token)\b", re.I)
PARAMS = {"q": "term", "query": "term", "search": "term", "term": "term", "username": "username", "password": "password"}


def snake(s):
    s = re.sub(r"(?<=[a-z0-9])([A-Z])", r"_\1", s).replace("-", "_").lower()
    return re.sub(r"\W+", "_", s).strip("_") or "value"


def flatten(o, path="$"):
    if isinstance(o, dict):
        for k, v in o.items():
            yield from flatten(v, f"{path}.{k}")
    elif isinstance(o, list):
        for i, v in enumerate(o[:3]):
            yield from flatten(v, f"{path}[{i}]")
    elif isinstance(o, (str, int)) and not isinstance(o, bool):
        yield path, o


def shape(path):
    p = re.sub(r"/\d+(?=/|$)", "/{id}", path.split("?")[0])
    p = re.sub(r"/(?=[A-Za-z0-9\-]*\d)[A-Za-z0-9\-]{9,}(?=/|$)", "/{id}", p)
    return p


def load(har_path):
    har = json.load(open(har_path, encoding="utf-8"))
    entries = har["log"]["entries"]
    from collections import Counter
    main = Counter(urlparse(e["request"]["url"]).netloc for e in entries).most_common(1)[0][0]
    recs, skipped = [], []
    for e in entries:
        rq, rs = e["request"], e["response"]
        u = urlparse(rq["url"])
        if u.netloc != main:
            continue
        label = f"{rq['method']} {shape(u.path)}"
        if STATIC.search(u.path):
            continue
        if AUTH.search(u.path):
            skipped.append((label, "authentication - handled by FRG - Authenticate"))
            continue
        c = rs.get("content", {})
        text = c.get("text") or ""
        if c.get("encoding") == "base64" and text:
            try:
                text = base64.b64decode(text).decode("utf-8", "replace")
            except Exception:
                text = ""
        mime = (c.get("mimeType") or "").lower()
        if rq["method"] == "GET" and not ("json" in mime or "html" in mime):
            continue
        pd = rq.get("postData") or {}
        recs.append({"method": rq["method"], "path": u.path + (("?" + u.query) if u.query else ""), "body": pd.get("text"),
                     "ctype": (pd.get("mimeType") or "").lower(), "status": rs.get("status", 200), "resp": text, "mime": mime,
                     "extract": [], "uses": []})
    return recs, skipped, main


def correlate(recs, min_digits, min_len):
    known = {}                     # value -> (var, record index, kind, expr)
    names = {}
    corr = []
    for j, r in enumerate(recs):
        # 1) replace values learned from earlier responses
        for val, (var, i, kind, expr) in list(known.items()):
            hit = False
            if val in r["path"]:
                r["path"] = r["path"].replace(val, "${%s}" % var); hit = True
            if r["body"] and val in r["body"]:
                r["body"] = r["body"].replace(val, "${%s}" % var); hit = True
            if hit:
                if (var, i) not in [(c["var"], c["from"]) for c in corr]:
                    corr.append({"var": var, "from": i, "kind": kind, "expr": expr, "used_in": []})
                    recs[i]["extract"].append((var, kind, expr))
                next(c for c in corr if c["var"] == var and c["from"] == i)["used_in"].append(j)
        # 2) parameterisation hints (framework CSV variables)
        r["path"] = re.sub(r"([?&])(q|query|search|term)=[^&$]*", r"\1\2=${term}", r["path"])
        if r["body"] and "json" in r["ctype"]:
            r["body"] = re.sub(r'("username"\s*:\s*)"[^"]*"', r'\1"${username}"', r["body"])
            r["body"] = re.sub(r'("password"\s*:\s*)"[^"]*"', r'\1"${password}"', r["body"])
        # 3) register candidates from this response
        txt = r["resp"]
        if "json" in r["mime"] and txt:
            try:
                for p, v in flatten(json.loads(txt)):
                    s = str(v)
                    ok = (isinstance(v, int) and len(s) >= min_digits) or (isinstance(v, str) and len(s) >= min_len)
                    if ok and s not in r["path"] and not (r["body"] and s in r["body"]):
                        parts = [x.split("[")[0] for x in p.split(".")[1:]]
                        base = snake(parts[-1]) if parts else "value"
                        if base in ("id", "uuid", "key", "value", "code") and len(parts) > 1:   # results[0].id -> result_id
                            base = snake(parts[-2].rstrip("s")) + "_" + base
                        names[base] = names.get(base, 0) + 1
                        var = base if names[base] == 1 else f"{base}_{names[base]}"
                        known[s] = (var, j, "json", p)
            except Exception:
                pass
        elif "html" in r["mime"] and txt:
            for m in re.finditer(r"""<input[^>]*name=["']([^"']+)["'][^>]*value=["']([^"']{6,})["']""", txt):
                var = snake(m.group(1))
                known[m.group(2)] = (var, j, "regex", r"""name=["']%s["'][^>]*value=["']([^"']+)["']""" % re.escape(m.group(1)))
    return corr


def ai_review(corr, recs):
    import llm
    if not llm.available():
        print("[ai] no ANTHROPIC_API_KEY - skipping review"); return {}
    payload = [{"var": c["var"], "extracted_from": f"{recs[c['from']]['method']} {shape(recs[c['from']]['path'])} {c['expr']}",
                "used_in": [f"{recs[u]['method']} {shape(recs[u]['path'])}" for u in c["used_in"]]} for c in corr]
    sysmsg = ("You review auto-detected correlations for a JMeter script. Rename variables to clear snake_case business names and "
              "list variables that are probably FALSE positives (not dynamic server-generated values, e.g. echoed constants). "
              'Answer ONE JSON object: {"rename": {"old": "new"}, "drop": ["var"], "notes": str}. Use only the given variables.')
    try:
        return llm.extract_json(llm.call(sysmsg, json.dumps(payload, indent=1), max_tokens=1200))
    except Exception as e:
        print(f"[ai] review skipped: {e}"); return {}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("har"); ap.add_argument("--name", default="recorded")
    ap.add_argument("--out", default="tests/fragments"); ap.add_argument("--tx-start", type=int, default=90)
    ap.add_argument("--min-digits", type=int, default=4); ap.add_argument("--min-len", type=int, default=6)
    ap.add_argument("--ai-review", action="store_true")
    a = ap.parse_args()
    recs, skipped, host = load(a.har)
    if not recs:
        sys.exit("no usable API requests found in the HAR")
    corr = correlate(recs, a.min_digits, a.min_len)
    review = ai_review(corr, recs) if a.ai_review and corr else {}
    ren, drop = review.get("rename", {}), set(review.get("drop", []))
    for c in corr:
        c["var"] = ren.get(c["var"], c["var"]) if c["var"] not in drop else c["var"]
    # apply review: renamed vars are rewritten everywhere; dropped vars keep recorded values (left as constants -> report only)
    for old, new in ren.items():
        for r in recs:
            r["path"] = r["path"].replace("${%s}" % old, "${%s}" % new)
            r["body"] = r["body"].replace("${%s}" % old, "${%s}" % new) if r["body"] else r["body"]
        for r in recs:
            r["extract"] = [(new if v == old else v, k, e) for v, k, e in r["extract"]]

    children = []
    for n, r in enumerate(recs):
        form = None
        body = r["body"]
        if body and "x-www-form-urlencoded" in r["ctype"]:
            form = dict(p.split("=", 1) if "=" in p else (p, "") for p in body.split("&")); body = None
        kids = [code_ok()]
        for var, kind, expr in r["extract"]:
            kids.append(json_ex(f"Extract - {var}", var, expr) if kind == "json" else regex_ex(f"Extract - {var}", var, expr))
        kids.append(think_timer())
        name = f"{r['method']} {shape(r['path'])}"
        children.append(tc(f"TX{a.tx_start + n}_{r['method']}_{re.sub(r'[^A-Za-z0-9]+', '_', shape(r['path'])).strip('_')[:34]}",
                           [http(name, r["method"], r["path"], body=body, form=form, children=kids)]))
    out = os.path.join(a.out, f"frg_{snake(a.name)}.jmx")
    write_fragment_file(out, f"FRG - {a.name}", f"Generated from {os.path.basename(a.har)} by ai/har_to_fragment.py. "
                        "Review transaction grouping, think times and assertions.", children)

    rep = [f"# Correlation report: {a.name}", "", f"Host: `{host}` | requests kept: {len(recs)} | correlations: {len(corr)}", "",
           "## Correlations detected (value from an earlier response re-used in a later request)", "",
           "| Variable | Extracted from | Expression | Used in |", "|---|---|---|---|"]
    for c in corr:
        src = recs[c["from"]]
        rep.append(f"| `{c['var']}` | #{c['from']+1} {src['method']} {shape(src['path'])} | `{c['expr']}` | "
                   + ", ".join(f"#{u+1}" for u in c["used_in"]) + (" **(AI: probable false positive)**" if c["var"] in drop else "") + " |")
    if not corr:
        rep.append("| - | none found | | |")
    rep += ["", "## Skipped", ""] + [f"- `{l}`: {why}" for l, why in skipped] if skipped else []
    rep += ["", "## Parameterisation to review", "- `q/query/search/term` -> `${term}`, `username`/`password` -> CSV vars (auto).",
            "- Other recorded IDs/SKUs that are NOT correlated are constants: replace them with CSV columns (data/products.csv) so users differ.",
            "- Request headers (Authorization, cookies) were removed on purpose: the framework header manager supplies them."]
    if review.get("notes"):
        rep += ["", "## AI review notes", review["notes"]]
    open(os.path.splitext(out)[0] + ".correlation_report.md", "w").write("\n".join(rep) + "\n")
    print(f"wrote {out}\nwrote {os.path.splitext(out)[0]}.correlation_report.md\n")
    print("\n".join(rep))
    print(f"\nNext: add  <Include Controller> -> ${{__P(base_dir,.)}}/tests/fragments/{os.path.basename(out)}  to a thread group")


if __name__ == "__main__":
    main()
