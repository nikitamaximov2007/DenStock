"""Resolve one CSS property for one rendered element, default (non-hover) state.

Enough of the cascade to prove that a design-system class really wins: rules
from ``static/css/app.css``, descendant/child selectors over tag, class,
attribute and ``:not(.x)`` parts, specificity, then source order. State
pseudo-classes (``:hover``, ``:focus``...) never apply, so a colour that only
appears on hover is reported as the non-hover value it really has. Rules
inside media queries count as applying: one check then covers every width.
"""

import re
from html.parser import HTMLParser
from pathlib import Path

from django.conf import settings

VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source", "wbr"}


class Element:
    def __init__(self, tag, attrs, parent):
        self.tag = tag
        self.attrs = dict(attrs)
        self.classes = set((self.attrs.get("class") or "").split())
        self.parent = parent
        self.text = ""


class _Tree(HTMLParser):
    def __init__(self):
        super().__init__()
        self.stack = []
        self.elements = []

    def handle_starttag(self, tag, attrs):
        element = Element(tag, attrs, self.stack[-1] if self.stack else None)
        self.elements.append(element)
        if tag not in VOID:
            self.stack.append(element)

    def handle_endtag(self, tag):
        for index in range(len(self.stack) - 1, -1, -1):
            if self.stack[index].tag == tag:
                del self.stack[index:]
                return

    def handle_data(self, data):
        for element in self.stack:
            element.text += data


def find_element(html, tag, text):
    tree = _Tree()
    tree.feed(html)
    matches = [el for el in tree.elements if el.tag == tag and el.text.strip() == text]
    assert len(matches) == 1, f"expected one <{tag}> {text!r}, found {len(matches)}"
    return matches[0]


_COMPOUND = re.compile(r"([a-z][a-z0-9-]*|\*)?((?:\.[\w-]+|\[[^\]]+\]|:not\(\.[\w-]+\))*)$")
_PART = re.compile(r"\.([\w-]+)|\[([\w-]+)(?:=\"?([^\"\]]*)\"?)?\]|:not\(\.([\w-]+)\)")


def _compound_matches(compound, element):
    parsed = _COMPOUND.match(compound)
    if parsed is None:
        return None
    tag, rest = parsed.groups()
    if tag and tag != "*" and tag != element.tag:
        return False
    for cls, attr, value, not_cls in _PART.findall(rest):
        if cls and cls not in element.classes:
            return False
        if attr and (attr not in element.attrs or (value and element.attrs[attr] != value)):
            return False
        if not_cls and not_cls in element.classes:
            return False
    return True


def _selector_matches(selector, element):
    compounds = selector.replace(">", " ").split()
    if not compounds or _compound_matches(compounds[-1], element) is not True:
        return False
    node = element.parent
    for compound in reversed(compounds[:-1]):
        while node is not None and _compound_matches(compound, node) is not True:
            node = node.parent
        if node is None:
            return False
        node = node.parent
    return True


def _specificity(selector):
    ids = selector.count("#")
    classes = len(re.findall(r"\.[\w-]+|\[[^\]]+\]|:not\(", selector))
    tags = len(re.findall(r"(?:^|[\s>])([a-z][a-z0-9-]*)", selector))
    return (ids, classes, tags)


def stylesheet_rules(path=None):
    path = path or Path(settings.BASE_DIR) / "static" / "css" / "app.css"
    source = re.sub(r"/\*.*?\*/", "", path.read_text(encoding="utf-8"), flags=re.S)
    for order, match in enumerate(re.finditer(r"([^{}]+)\{([^{}]*)\}", source)):
        selectors, body = match.groups()
        declarations = {}
        for declaration in body.split(";"):
            if ":" in declaration:
                name, value = declaration.split(":", 1)
                declarations[name.strip()] = value.strip()
        for selector in selectors.split(","):
            yield order, selector.strip(), declarations


def resolved(element, prop, *, shorthand=None):
    """Winning value of ``prop`` in the default state, with the rule that won."""
    best = None
    for order, selector, declarations in stylesheet_rules():
        if selector.startswith("@") or ":" in selector.replace(":not(", ""):
            continue  # at-rule headers and state pseudo-classes never apply here
        names = [name for name in declarations if name in (prop, shorthand)]
        if not names or not _selector_matches(selector, element):
            continue
        rank = (_specificity(selector), order)
        if best is None or rank >= best[0]:
            best = (rank, declarations[names[-1]], selector)  # last declaration in the rule
    return (best[1], best[2]) if best else (None, None)
