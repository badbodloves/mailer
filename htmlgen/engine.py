"""Core engine — assemble layout + blocks into finished HTML templates."""

import random
from pathlib import Path
from .config import load_config
from .constraints import parse_tags, check_constraints
from .placeholders import resolve_engine_placeholders, apply_placeholder_mapping

_BASE = Path(__file__).parent
MAX_RETRIES = 200
BLOCK_NAMES = ["logo", "referenz", "satz", "hinweis", "frist", "link", "gruss", "footer"]


def _load_variants(block_name: str, base_dir: Path | None = None) -> list[dict]:
    """Load all HTML variant files for a block."""
    block_dir = (base_dir or _BASE) / "blocks" / block_name
    if not block_dir.is_dir():
        return []

    variants = []
    for f in sorted(block_dir.glob("*.html")):
        content = f.read_text(encoding="utf-8")
        tags = parse_tags(content)
        variants.append({
            "variant": f.stem,
            "tags": tags,
            "html": content,
        })
    return variants


def _load_layouts(base_dir: Path | None = None) -> list[dict]:
    """Load all layout templates."""
    layout_dir = (base_dir or _BASE) / "layouts"
    if not layout_dir.is_dir():
        return []

    layouts = []
    for f in sorted(layout_dir.glob("*.html")):
        content = f.read_text(encoding="utf-8")
        layouts.append({
            "name": f.stem,
            "html": content,
        })
    return layouts


def _load_all(base_dir: Path | None = None) -> tuple[dict, list]:
    """Load all block variants and layouts once from disk."""
    base = base_dir or _BASE
    block_variants = {}
    for name in BLOCK_NAMES:
        block_variants[name] = _load_variants(name, base)
    layouts = _load_layouts(base)
    return block_variants, layouts


def _pick_blocks(block_variants: dict, disabled: set, constraints: list) -> dict:
    """Pick one variant per enabled block, respecting constraints."""
    for _ in range(MAX_RETRIES):
        selection = {}
        for block_name, variants in block_variants.items():
            if block_name in disabled or not variants:
                continue
            v = random.choice(variants)
            selection[block_name] = v

        if check_constraints(selection, constraints):
            return selection

    return selection


def _assemble(cfg: dict, layout: dict, selection: dict) -> str:
    """Assemble a single template from layout + block selection."""
    html = layout["html"]
    for block_name in BLOCK_NAMES:
        placeholder = "{BLOCK_" + block_name.upper() + "}"
        if block_name in selection:
            block_html = selection[block_name]["html"]
            first_line = block_html.split("\n", 1)[0]
            if first_line.strip().startswith("<!--") and "tags:" in first_line.lower():
                block_html = block_html.split("\n", 1)[1] if "\n" in block_html else ""
            html = html.replace(placeholder, block_html)
        else:
            html = html.replace(placeholder, "")

    html = resolve_engine_placeholders(html, cfg)
    html = apply_placeholder_mapping(html, cfg)
    return html


def generate_one(cfg: dict, base_dir: Path | None = None,
                 _cache: tuple | None = None) -> str:
    """Generate a single HTML template string.

    Pass _cache=(block_variants, layouts) to skip disk reads.
    """
    if _cache:
        block_variants, layouts = _cache
    else:
        block_variants, layouts = _load_all(base_dir)

    chosen_layout_name = cfg.get("layout")
    if chosen_layout_name:
        filtered = [l for l in layouts if l["name"] == chosen_layout_name]
        pick_from = filtered or layouts
    else:
        pick_from = layouts

    if not pick_from:
        raise FileNotFoundError("No layouts found")

    layout = random.choice(pick_from)
    blocks_cfg = cfg.get("blocks", {})
    disabled = {name for name, enabled in blocks_cfg.items() if not enabled}
    constraints = cfg.get("constraints", [])

    selection = _pick_blocks(block_variants, disabled, constraints)
    return _assemble(cfg, layout, selection)


def generate(config_path: str | Path | None = None,
             count: int | None = None,
             output_dir: str | Path | None = None,
             base_dir: Path | None = None) -> list[Path]:
    """Generate multiple HTML templates and write them to disk."""
    cfg = load_config(config_path)

    count = count or cfg.get("output", {}).get("count", 50)
    output_dir = Path(output_dir or cfg.get("output", {}).get("directory", "./output"))
    pattern = cfg.get("output", {}).get("filename_pattern", "template_{n:04d}.html")

    output_dir.mkdir(parents=True, exist_ok=True)

    cache = _load_all(base_dir)

    written = []
    for i in range(1, count + 1):
        html = generate_one(cfg, base_dir, _cache=cache)
        filename = pattern.format(n=i)
        path = output_dir / filename
        path.write_text(html, encoding="utf-8")
        written.append(path)

    return written
