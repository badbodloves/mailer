import re
import random


_STYLE_ATTR_RE = re.compile(r'(style\s*=\s*")([^"]*?)(")', re.IGNORECASE)
_PX_PROP_RE = re.compile(
    r"((?:padding|margin)(?:-(?:top|right|bottom|left))?\s*:\s*)([^;\"]+)",
    re.IGNORECASE,
)
_PX_NUM_RE = re.compile(r"(\d+)(px)", re.IGNORECASE)
_TAG_WITH_STYLE_RE = re.compile(
    r"(<(?:div|p|table|td|span|a|hr)\b)([^>]*?)(style\s*=\s*\"[^\"]*?\")([^>]*?>)",
    re.IGNORECASE,
)
_ELIGIBLE_TAG_RE = re.compile(
    r"(<(?:div|p|table|td|span|a|hr)\b)([^>]*?>)",
    re.IGNORECASE,
)
_IMG_TAG_RE = re.compile(r"(<img\b)(\s[^>]*?)(/?>)", re.IGNORECASE)
_ATTR_RE = re.compile(r"""(\w[\w-]*)\s*=\s*(?:"[^"]*"|'[^']*'|\S+)""", re.IGNORECASE)

PREFIXES = [
    "content", "main", "info", "msg", "block", "section", "wrapper",
    "container", "box", "panel", "area", "module", "item", "group",
    "row", "cell", "txt", "hdr", "ftr", "notice",
]
SUFFIXES = [
    "primary", "inner", "top", "base", "core", "data", "body", "head",
    "sub", "alt", "new", "ext", "wrap", "layout", "frame", "view",
    "col", "set", "part", "line",
]


class AntiFingerprintEngine:
    def __init__(self, enable_classes: bool = True):
        self._enable_classes = enable_classes

    def transform(self, html: str) -> str:
        html = _swap_tags(html)
        html = _vary_pixels(html)
        html = _shuffle_css_properties(html)
        html = _shuffle_image_attributes(html)
        if self._enable_classes:
            html = _inject_classes(html)
        return html


def _swap_tags(html: str) -> str:
    pairs = [
        ("strong", "b"),
        ("em", "i"),
    ]
    for tag_a, tag_b in pairs:
        html = _swap_one_pair(html, tag_a, tag_b)
    return html


def _swap_one_pair(html: str, tag_a: str, tag_b: str) -> str:
    tokens = re.split(
        rf"(</?(?:{tag_a}|{tag_b})\b[^>]*>)", html, flags=re.IGNORECASE
    )
    stack: list = []
    out = []
    for token in tokens:
        m = re.match(rf"<(/?)(?:{tag_a}|{tag_b})\b", token, re.IGNORECASE)
        if not m:
            out.append(token)
            continue
        is_close = m.group(1) == "/"
        if not is_close:
            do_swap = random.random() < 0.5
            stack.append(do_swap)
            if do_swap:
                token = _flip_tag(token, tag_a, tag_b)
        else:
            do_swap = stack.pop() if stack else False
            if do_swap:
                token = _flip_tag(token, tag_a, tag_b)
        out.append(token)
    return "".join(out)


def _flip_tag(token: str, tag_a: str, tag_b: str) -> str:
    lo = token.lower()
    if tag_a in lo:
        return re.sub(tag_a, tag_b, token, count=1, flags=re.IGNORECASE)
    return re.sub(tag_b, tag_a, token, count=1, flags=re.IGNORECASE)


def _vary_pixels(html: str) -> str:
    def _vary_style(match: re.Match) -> str:
        prefix, css, suffix = match.group(1), match.group(2), match.group(3)
        css = _PX_PROP_RE.sub(_vary_prop, css)
        return prefix + css + suffix

    return _STYLE_ATTR_RE.sub(_vary_style, html)


def _vary_prop(match: re.Match) -> str:
    prop_prefix = match.group(1)
    value_part = match.group(2)

    def _vary_num(m: re.Match) -> str:
        val = int(m.group(1))
        if val <= 4:
            return m.group(0)
        if random.random() < 0.3:
            val = max(0, val + random.choice([-2, -1, 1, 2]))
        return f"{val}px"

    return prop_prefix + _PX_NUM_RE.sub(_vary_num, value_part)


def _shuffle_css_properties(html: str) -> str:
    def _shuffle_one(match: re.Match) -> str:
        prefix, css, suffix = match.group(1), match.group(2), match.group(3)
        parts = [p.strip() for p in css.split(";") if p.strip()]
        if len(parts) > 1:
            random.shuffle(parts)
        return prefix + ";".join(parts) + ";" + suffix

    return _STYLE_ATTR_RE.sub(_shuffle_one, html)


def _shuffle_image_attributes(html: str) -> str:
    def _shuffle_one(match: re.Match) -> str:
        tag_open = match.group(1)
        attrs_str = match.group(2)
        tag_close = match.group(3)

        attr_spans = list(_ATTR_RE.finditer(attrs_str))
        if len(attr_spans) <= 1:
            return match.group(0)

        attr_strings = [attrs_str[m.start():m.end()] for m in attr_spans]
        random.shuffle(attr_strings)
        return tag_open + " " + " ".join(attr_strings) + " " + tag_close

    return _IMG_TAG_RE.sub(_shuffle_one, html)


def _inject_classes(html: str) -> str:
    inject_rate = random.uniform(0.25, 0.50)
    style_to_class: dict = {}
    injections: list = []

    def _maybe_inject(match: re.Match) -> str:
        full = match.group(0)
        if re.search(r'\bclass\s*=', full, re.IGNORECASE):
            return full

        style_match = re.search(r'style\s*=\s*"([^"]*?)"', full, re.IGNORECASE)
        if not style_match:
            return full

        if random.random() > inject_rate:
            return full

        style_val = style_match.group(1).strip()
        if not style_val:
            return full

        sorted_key = ";".join(sorted(p.strip() for p in style_val.split(";") if p.strip()))
        if sorted_key in style_to_class:
            cls_name = style_to_class[sorted_key]
        else:
            cls_name = _make_class_name()
            style_to_class[sorted_key] = cls_name
            injections.append((cls_name, style_val))

        insert_pos = match.end(1)
        offset = insert_pos - match.start()
        before = full[:offset]
        after = full[offset:]
        return before + f' class="{cls_name}"' + after

    html = _ELIGIBLE_TAG_RE.sub(_maybe_inject, html)

    if injections:
        style_block = "\n<style>\n"
        for cls_name, css in injections:
            style_block += f"  .{cls_name} {{ {css} }}\n"
        style_block += "</style>\n"

        meta_match = re.search(
            r'(<meta\s+charset\s*=\s*"[^"]*"\s*/?>)', html, re.IGNORECASE
        )
        if meta_match:
            pos = meta_match.end()
            html = html[:pos] + style_block + html[pos:]
        else:
            head_close = re.search(r"</head>", html, re.IGNORECASE)
            if head_close:
                html = html[: head_close.start()] + style_block + html[head_close.start() :]
            else:
                html = style_block + html

    return html


def _make_class_name() -> str:
    return (
        f"{random.choice(PREFIXES)}-"
        f"{random.choice(SUFFIXES)}-"
        f"{random.randint(10, 99)}"
    )
