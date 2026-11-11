"""WCAG contrast of the /v2 dashboard's colour tokens, in both themes.

Reads the tokens straight from webui_app/src/v2/guardian.css (the navy
default in `:root{...}`, the light theme in `:root[data-theme="light"]{...}`)
and checks every pair the page draws: text at least 4.5:1, and marks, lines
and control borders that carry meaning at least 3:1.

    python tests/test_v2_contrast.py      prints every pair and its ratio
"""

import os
import re

CSS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                   "webui_app", "src", "v2", "guardian.css")

TEXT, MARK = 4.5, 3.0

# (foreground, background, minimum, where it is drawn)
PAIRS = [
    # body text on every surface
    *[(fg, bg, TEXT, "text") for fg in ("ink", "ink2")
      for bg in ("bg", "surface", "well", "sunk")],
    *[("ink3", bg, TEXT, "faint text") for bg in ("bg", "surface", "well")],
    ("surface", "ink", TEXT, "primary button, paused run-state, tooltip, toast"),
    # real money: the band, the Real money tag, the go-LIVE button
    ("on-real", "real-fill", TEXT, "text on the LIVE band and tags"),
    # practice
    ("prac-ink", "prac-tint", TEXT, "Practice tag, band word"),
    ("prac-ink", "surface", TEXT, "practice words"),
    ("ink", "prac-tint", TEXT, "practice band sentence chip"),
    ("ink", "prac-stripe", TEXT, "practice hatch"),
    # unknown
    ("ink", "unk-tint", TEXT, "unknown band chip"),
    ("ink2", "unk-tint", TEXT, "as-of stamp, unknown tag"),
    ("ink", "unk-stripe", TEXT, "unknown hatch"),
    # P&L: green / red text on the number, wherever a result is shown
    *[("gain", bg, TEXT, "gain") for bg in ("bg", "surface", "well", "sunk")],
    *[("bad", bg, TEXT, "loss / fault") for bg in ("bg", "surface", "well",
                                                     "sunk", "bad-tint")],
    ("ink", "bad-tint", TEXT, "fault note"),
    ("surface", "bad", TEXT, "problem badge"),
    # heads-up
    ("warn-ink", "warn-tint", TEXT, "heads-up note"),
    ("ink", "warn-tint", TEXT, "heads-up note"),
    # marks, lines and borders that carry meaning
    *[("line2", bg, MARK, "control border, chart zero line")
      for bg in ("bg", "surface", "well")],
    *[("real", bg, MARK, "real-money line, frame, edge")
      for bg in ("bg", "surface", "well")],
    *[("prac", bg, MARK, "practice line, dashed edge")
      for bg in ("bg", "surface", "well", "prac-tint")],
    ("unk", "surface", MARK, "unknown edge"),
    ("unk", "unk-tint", MARK, "unknown edge"),
    ("bad", "bad-tint", MARK, "fault edge"),
    ("warn-line", "warn-tint", MARK, "heads-up edge"),
    *[("gain-mark", bg, MARK, "winning day mark") for bg in ("surface", "well")],
    *[("bad-mark", bg, MARK, "losing day mark") for bg in ("surface", "well")],
]


def _block(css, selector):
    m = re.search(re.escape(selector) + r"\s*\{(.*?)\}", css, re.S)
    assert m, f"{selector} not found in guardian.css"
    return dict(re.findall(r"--([\w-]+)\s*:\s*(#[0-9A-Fa-f]{6})\b", m.group(1)))


def themes():
    with open(CSS, encoding="utf-8") as f:
        css = re.sub(r"/\*.*?\*/", "", f.read(), flags=re.S)
    navy = _block(css, ":root")
    # the light theme overrides; anything it leaves out stays navy
    light = {**navy, **_block(css, ':root[data-theme="light"]')}
    return {"navy": navy, "light": light}


def _lum(h):
    c = [int(h[i:i + 2], 16) / 255 for i in (1, 3, 5)]
    c = [x / 12.92 if x <= 0.04045 else ((x + 0.055) / 1.055) ** 2.4 for x in c]
    return 0.2126 * c[0] + 0.7152 * c[1] + 0.0722 * c[2]


def ratio(a, b):
    hi, lo = sorted((_lum(a), _lum(b)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


def rows():
    out = []
    for name, t in themes().items():
        for fg, bg, need, where in PAIRS:
            out.append((name, fg, bg, ratio(t[fg], t[bg]), need, where))
    return out


def test_every_token_pair_meets_its_minimum():
    bad = [f"{n}: --{fg} on --{bg} is {r:.2f}:1, needs {need}:1 ({where})"
           for n, fg, bg, r, need, where in rows() if r < need]
    assert not bad, "\n".join(bad)


def test_both_themes_define_every_colour_token():
    t = themes()
    with open(CSS, encoding="utf-8") as f:
        css = re.sub(r"/\*.*?\*/", "", f.read(), flags=re.S)
    light = _block(css, ':root[data-theme="light"]')
    assert set(light) == set(_block(css, ":root")), \
        "the light theme must set every colour the navy theme sets"
    assert t["navy"]["real"] != t["navy"]["bad"]


def test_real_money_is_blue_and_never_the_colour_of_a_loss():
    for name, t in themes().items():
        for k in ("real", "real-fill"):
            r, g, b = (int(t[k][i:i + 2], 16) for i in (1, 3, 5))
            assert b > r and b > g, f"{name}: --{k} {t[k]} is not blue"
            assert t[k].lower() not in (t["bad"].lower(), t["bad-mark"].lower())


def test_text_is_softened_not_pure_white():
    t = themes()["navy"]
    assert t["ink"].upper() != "#FFFFFF"
    assert 12 <= ratio(t["ink"], t["surface"]) <= 14.5


if __name__ == "__main__":
    for n, fg, bg, r, need, where in rows():
        flag = "ok  " if r >= need else "FAIL"
        print(f"{flag} {n:5} --{fg:10} on --{bg:11} {r:5.2f}:1 (>= {need}) {where}")
