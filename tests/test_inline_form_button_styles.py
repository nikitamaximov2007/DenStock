"""Design-system buttons inside ``.inline-form`` keep their own style.

``.inline-form button`` (class + element, specificity 0,1,1) reset bare
buttons, but it also beat single-class variants such as ``.btn--primary``
(0,1,0): a primary or danger button inside an inline form rendered white
text on a white background until hover. The reset now applies only to
buttons without ``.btn`` and, through ``:where()``, keeps its old specificity
for them, so bare buttons look exactly as before.

The resolver below covers what app.css needs: descendant/child selectors over
tag, class, ``:not(.x)`` and ``:where(:not(.x))`` parts, specificity, then
source order. State pseudo-classes (hover, focus...) never apply.
"""
import re
from pathlib import Path

import pytest
from django.conf import settings

CSS_PATH = Path(settings.BASE_DIR) / "static" / "css" / "app.css"
TEMPLATES = Path(settings.BASE_DIR) / "templates"
OLD_RULE = ".inline-form button {"

_COMPOUND = re.compile(
    r"([a-z][a-z0-9-]*|\*)?((?:\.[\w-]+|:not\(\.[\w-]+\)|:where\(:not\(\.[\w-]+\)\))*)$"
)
_PART = re.compile(r"(:where\()?:not\(\.([\w-]+)\)\)?|\.([\w-]+)")


class El:
    def __init__(self, tag, classes, parent=None):
        self.tag, self.classes, self.parent = tag, set(classes.split()), parent


def _compound(compound, el):
    parsed = _COMPOUND.match(compound)
    if parsed is None:
        return False
    tag, rest = parsed.groups()
    if tag and tag not in ("*", el.tag):
        return False
    for _where, not_cls, cls in _PART.findall(rest):
        if cls and cls not in el.classes:
            return False
        if not_cls and not_cls in el.classes:
            return False
    return True


def _matches(selector, el):
    parts = selector.replace(">", " ").split()
    if not parts or not _compound(parts[-1], el):
        return False
    node = el.parent
    for part in reversed(parts[:-1]):
        while node is not None and not _compound(part, node):
            node = node.parent
        if node is None:
            return False
        node = node.parent
    return True


def _specificity(selector):
    counted = re.sub(r":where\((?:[^()]|\([^()]*\))*\)", "", selector)
    classes = len(re.findall(r"\.[\w-]+", re.sub(r":not\(", "", counted)))
    tags = len(re.findall(r"(?:^|[\s>])([a-z][a-z0-9-]*)", counted))
    return (counted.count("#"), classes, tags)


def _rules(css):
    css = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
    for order, (selectors, body) in enumerate(re.findall(r"([^{}]+)\{([^{}]*)\}", css)):
        decls = {}
        for decl in body.split(";"):
            if ":" in decl:
                name, value = decl.split(":", 1)
                decls[name.strip()] = value.strip()
        for selector in selectors.split(","):
            yield order, selector.strip(), decls


def resolved(css, el, prop, shorthand=None):
    best = None
    for order, selector, decls in _rules(css):
        stateless = re.sub(r":where\((?:[^()]|\([^()]*\))*\)|:not\(\.[\w-]+\)", "", selector)
        if selector.startswith("@") or ":" in stateless:
            continue
        names = [n for n in decls if n in (prop, shorthand)]
        if names and _matches(selector, el):
            rank = (_specificity(selector), order)
            if best is None or rank >= best[0]:
                best = (rank, decls[names[-1]])
    return best[1] if best else None


def _inside_inline_form(classes):
    return El("button", classes, El("form", "inline-form", El("div", "card")))


def _outside(classes):
    return El("button", classes, El("form", "form-actions", El("div", "card")))


def _audited_btn_class_sets():
    """Every ``.btn`` button that sits inside an ``inline-form`` form in templates."""
    found = []
    for path in sorted(TEMPLATES.rglob("*.html")):
        source = path.read_text(encoding="utf-8")
        for attrs, body in re.findall(r"<form\b([^>]*)>(.*?)</form>", source, re.S):
            form_class = re.search(r'class="([^"]*)"', attrs)
            if not form_class or "inline-form" not in form_class.group(1).split():
                continue
            for button in re.findall(r"<button\b([^>]*)>", body, re.S):
                cls = re.search(r'class="([^"]*)"', button)
                if cls and "btn" in cls.group(1).split():
                    found.append((str(path.relative_to(TEMPLATES)), cls.group(1)))
    return found


PROPS = [
    ("background-color", "background"),
    ("border-color", "border"),
    ("padding", None),
    ("border-radius", None),
]


@pytest.fixture(scope="module")
def css():
    return CSS_PATH.read_text(encoding="utf-8")


def test_the_audit_still_finds_the_inline_form_design_system_buttons():
    audited = _audited_btn_class_sets()
    assert len(audited) >= 33
    assert any("btn--primary" in cls for _path, cls in audited)
    assert any("btn--danger" in cls for _path, cls in audited)


@pytest.mark.parametrize("classes", sorted({cls for _p, cls in _audited_btn_class_sets()}))
def test_btn_inside_inline_form_resolves_like_outside(css, classes):
    for prop, shorthand in PROPS:
        inside = resolved(css, _inside_inline_form(classes), prop, shorthand)
        outside = resolved(css, _outside(classes), prop, shorthand)
        assert inside == outside, f"{classes}: {prop} {outside!r} became {inside!r}"


@pytest.mark.parametrize(
    ("classes", "background"),
    [("btn btn--primary", "var(--accent)"), ("btn btn--danger", "var(--danger)")],
)
def test_primary_and_danger_are_filled_not_white(css, classes, background):
    button = _inside_inline_form(classes)
    assert resolved(css, button, "background-color", "background") == background
    assert resolved(css, button, "color") == "#fff"


def test_bare_buttons_keep_the_inline_form_reset(css):
    bare = _inside_inline_form("")
    assert resolved(css, bare, "background-color", "background") == "#fff"
    assert resolved(css, bare, "padding") == "6px 10px"
    assert resolved(css, bare, "border-radius") == "6px"
    # later rules of the same (0,1,1) weight still win by source order, as before
    photo = El("button", "", El("form", "inline-form", El("div", "photo-actions")))
    assert resolved(css, photo, "padding") == "3px 7px"
    toolbar = El("button", "", El("form", "inline-form", El("div", "toolbar")))
    assert resolved(css, toolbar, "padding") == "8px 10px"


def test_the_reset_keeps_its_old_specificity_for_bare_buttons(css):
    assert OLD_RULE not in css
    assert _specificity(".inline-form button:where(:not(.btn))") == _specificity(
        ".inline-form button"
    )


def test_negative_control_old_rule_made_primary_white(css):
    old_css = css.replace(".inline-form button:where(:not(.btn)) {", OLD_RULE, 1)
    assert old_css != css
    button = _inside_inline_form("btn btn--primary")
    assert resolved(old_css, button, "background-color", "background") == "#fff"
    assert resolved(old_css, button, "color") == "#fff"
