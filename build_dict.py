#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Build Rime custom dictionaries from the loose files in ``dict/``.

The input files are deliberately treated as line-oriented data instead of
being loaded by a YAML parser.  Rime dictionaries only need a small subset of
YAML here, and this keeps the builder usable without third-party packages.

Each output code is in the form used by the Rime dictionary in this repository:
``pinyin-with-tone;aux-code`` for every character.  When a source line has no
pinyin, the best reading from ``zi.pro.dict.yaml`` is used.  Existing output
entries are treated as an optional reviewed reference, so corrected entries
survive later incremental builds.
"""

from __future__ import annotations

import argparse
import os
import pickle
import re
import sys
import tempfile
import unicodedata
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterator, Sequence


ROOT = Path(__file__).resolve().parent
DEFAULT_INPUT_DIR = ROOT / "dict"
DEFAULT_OUTPUT_DIR = ROOT / "patch" / "custom_dict"
DEFAULT_ZI_DICT = Path(r"C:\Users\30789\AppData\Roaming\Rime\dicts\zi.pro.dict.yaml")
DEFAULT_CACHE = ROOT / ".build" / "build_dict" / "zi.pro.pkl"
DEFAULT_REPORT = DEFAULT_OUTPUT_DIR / "build_report.md"

CACHE_VERSION = 2
NUMERIC_RE = re.compile(r"^[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?$")
TRAILING_COMMENT_RE = re.compile(r"\s+#.*$")

# Full-pinyin finals used only to split an occasional tab-less ``nihao`` field.
PINYIN_FINALS = {
    "a", "ai", "an", "ang", "ao", "e", "ei", "en", "eng", "er", "o",
    "ong", "ou", "i", "ia", "ian", "iang", "iao", "ie", "in", "ing",
    "iong", "iu", "u", "ua", "uai", "uan", "uang", "ue", "ui", "un",
    "uo", "v", "ve", "van", "vn",
}
PINYIN_INITIALS = (
    "zh", "ch", "sh", "b", "p", "m", "f", "d", "t", "n", "l", "g",
    "k", "h", "j", "q", "x", "r", "z", "c", "s", "y", "w",
)

# Tone marks occur in zi.pro and in some hand-written source files.  Mapping
# them explicitly also preserves ü, which Unicode decomposition would lose.
TONE_BASES = str.maketrans({
    "ā": "a", "á": "a", "ǎ": "a", "à": "a",
    "ē": "e", "é": "e", "ě": "e", "è": "e",
    "ī": "i", "í": "i", "ǐ": "i", "ì": "i",
    "ō": "o", "ó": "o", "ǒ": "o", "ò": "o",
    "ū": "u", "ú": "u", "ǔ": "u", "ù": "u",
    "ǖ": "v", "ǘ": "v", "ǚ": "v", "ǜ": "v", "ü": "v",
    "ń": "n", "ň": "n", "ǹ": "n", "ḿ": "m", "ṁ": "m",
})


@dataclass(frozen=True)
class Reading:
    pinyin: str
    aux: str
    weight: float

    @property
    def label(self) -> str:
        code = f"{self.pinyin};{self.aux}" if self.aux else self.pinyin
        return f"{code} ({format_weight(self.weight)})"


@dataclass(frozen=True)
class ReferenceEntry:
    codes: str
    weight: str | None = None


@dataclass
class BuildStats:
    source_files: int = 0
    source_entries: int = 0
    written_entries: int = 0
    skipped_entries: int = 0
    malformed_entries: int = 0
    referenced_entries: int = 0
    explicit_pinyin_entries: int = 0
    inferred_pinyin_entries: int = 0
    unknown_chars: Counter[str] | None = None
    ambiguous: dict[str, dict] | None = None

    def __post_init__(self) -> None:
        if self.unknown_chars is None:
            self.unknown_chars = Counter()
        if self.ambiguous is None:
            self.ambiguous = {}


def format_weight(value: float) -> str:
    """Render a dictionary weight without an unnecessary decimal suffix."""

    if value == int(value):
        return str(int(value))
    return f"{value:g}"


def parse_weight(value: str | None) -> float:
    if not value:
        return 0.0
    value = value.strip()
    if not NUMERIC_RE.fullmatch(value):
        return 0.0
    try:
        return float(value)
    except ValueError:
        return 0.0


def is_numeric(value: str | None) -> bool:
    return bool(value and NUMERIC_RE.fullmatch(value.strip()))


def strip_tone(value: str) -> str:
    """Normalize pinyin spelling while retaining ``v`` for ü."""

    value = value.strip().lower().replace("u:", "v").replace("ü", "v")
    value = value.translate(TONE_BASES)
    value = unicodedata.normalize("NFD", value)
    value = "".join(char for char in value if unicodedata.category(char) != "Mn")
    value = value.replace("u:", "v").replace("ü", "v")
    value = re.sub(r"[1-5]$", "", value)
    return value.replace("'", "").replace(" ", "")


def _iter_data_lines(path: Path, encoding: str = "utf-8") -> Iterator[tuple[int, str]]:
    """Yield data lines after the Rime header delimiter."""

    in_data = False
    with path.open("r", encoding=encoding, errors="replace", newline="") as handle:
        for line_number, raw in enumerate(handle, 1):
            line = raw.rstrip("\r\n")
            stripped = line.strip()
            if not in_data:
                if stripped.lstrip("\ufeff") == "...":
                    in_data = True
                continue
            # Rime files often use a second ``...`` to terminate the header
            # block.  It is not a dictionary entry.
            if stripped == "...":
                continue
            if not stripped or stripped.startswith("#"):
                continue
            yield line_number, line


def _parse_character_line(line: str) -> tuple[str, Reading] | None:
    fields = line.split("\t")
    if len(fields) < 2:
        fields = line.split(None, 2)
    if len(fields) < 2:
        return None
    character = fields[0].strip()
    code = fields[1].strip()
    if len(character) != 1 or not code:
        return None
    pinyin, separator, aux = code.partition(";")
    pinyin = pinyin.strip()
    aux = aux.strip() if separator else ""
    if not pinyin:
        return None
    weight = parse_weight(fields[2] if len(fields) > 2 else None)
    return character, Reading(pinyin=pinyin, aux=aux, weight=weight)


def _sort_readings(readings: dict[str, list[Reading]]) -> None:
    for character, values in readings.items():
        # Keep the first occurrence for ties.  zi.pro is already frequency
        # ordered, so this is deterministic even when weights are equal.
        values.sort(key=lambda reading: reading.weight, reverse=True)
        unique: list[Reading] = []
        seen: set[tuple[str, str]] = set()
        for reading in values:
            # Tone variants are distinct readings (e.g. ``a``/``ā``/``á``),
            # so retain them for both selection and the manual-review report.
            key = (reading.pinyin, reading.aux)
            if key not in seen:
                seen.add(key)
                unique.append(reading)
        readings[character] = unique


def _load_cache(cache_path: Path, source: Path) -> dict[str, list[Reading]] | None:
    try:
        source_stat = source.stat()
        with cache_path.open("rb") as handle:
            payload = pickle.load(handle)
        if (
            payload.get("version") != CACHE_VERSION
            or payload.get("size") != source_stat.st_size
            or payload.get("mtime_ns") != source_stat.st_mtime_ns
        ):
            return None
        result: dict[str, list[Reading]] = {}
        for character, values in payload.get("readings", {}).items():
            result[character] = [Reading(*value) for value in values]
        return result
    except (OSError, EOFError, KeyError, TypeError, ValueError, pickle.PickleError):
        return None


def _write_cache(cache_path: Path, source: Path, readings: dict[str, list[Reading]]) -> None:
    try:
        source_stat = source.stat()
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": CACHE_VERSION,
            "size": source_stat.st_size,
            "mtime_ns": source_stat.st_mtime_ns,
            "readings": {
                character: [(item.pinyin, item.aux, item.weight) for item in values]
                for character, values in readings.items()
            },
        }
        with tempfile.NamedTemporaryFile(
            "wb", dir=cache_path.parent, prefix=cache_path.name + ".", delete=False
        ) as handle:
            pickle.dump(payload, handle, protocol=pickle.HIGHEST_PROTOCOL)
            temporary = Path(handle.name)
        os.replace(temporary, cache_path)
    except OSError:
        # A read-only checkout should still be able to build the dictionary.
        try:
            if "temporary" in locals():
                temporary.unlink(missing_ok=True)
        except OSError:
            pass


def load_character_dictionary(
    source: Path,
    cache_path: Path | None = DEFAULT_CACHE,
    use_cache: bool = True,
    encoding: str = "utf-8",
) -> dict[str, list[Reading]]:
    """Load the character table in one streaming pass, with an optional cache."""

    source = Path(source)
    if not source.is_file():
        raise FileNotFoundError(f"character dictionary not found: {source}")
    if use_cache and cache_path:
        cached = _load_cache(Path(cache_path), source)
        if cached is not None:
            return cached

    readings: dict[str, list[Reading]] = defaultdict(list)
    for _, line in _iter_data_lines(source, encoding):
        parsed = _parse_character_line(line)
        if parsed is None:
            continue
        character, reading = parsed
        readings[character].append(reading)
    _sort_readings(readings)
    result = dict(readings)
    if use_cache and cache_path:
        _write_cache(Path(cache_path), source, result)
    return result


def is_cjk(character: str) -> bool:
    codepoint = ord(character)
    return (
        0x3400 <= codepoint <= 0x4DBF
        or 0x4E00 <= codepoint <= 0x9FFF
        or 0xF900 <= codepoint <= 0xFAFF
        or 0x20000 <= codepoint <= 0x323AF
    )


def _unquote(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        return value[1:-1]
    return value


def _parse_source_line(line: str) -> tuple[str, list[str], str | None] | None:
    """Return ``word, optional pinyin tokens, optional weight``."""

    line = TRAILING_COMMENT_RE.sub("", line).rstrip()
    if not line:
        return None
    fields = line.split("\t")
    if len(fields) == 1:
        parts = line.split()
        if not parts:
            return None
        fields = [parts[0]]
        if len(parts) > 1:
            # Also accept the common tab-less form ``word pinyin... weight``.
            if is_numeric(parts[-1]) and len(parts) > 2:
                fields.extend([" ".join(parts[1:-1]), parts[-1]])
            else:
                fields.append(" ".join(parts[1:]))
    word = _unquote(fields[0].strip())
    if not word:
        return None

    pinyin_field: str | None = None
    weight: str | None = None
    if len(fields) >= 2:
        second = fields[1].strip()
        if is_numeric(second):
            weight = second
        elif second:
            pinyin_field = second
    if len(fields) >= 3:
        if is_numeric(fields[2]):
            weight = fields[2].strip()
        elif pinyin_field is None and fields[2].strip():
            pinyin_field = fields[2].strip()
    tokens = pinyin_field.split() if pinyin_field else []
    return word, tokens, weight


def _load_reference_dictionary(
    path: Path,
    encoding: str = "utf-8",
) -> dict[str, ReferenceEntry]:
    """Load reviewed output entries without loading the whole file as YAML."""

    if not path.is_file():
        return {}
    entries: dict[str, ReferenceEntry] = {}
    for _, line in _iter_data_lines(path, encoding):
        fields = line.split("\t")
        if len(fields) < 2:
            continue
        word = fields[0].strip()
        codes = fields[1].strip()
        if not word or not codes:
            continue
        weight = fields[2].strip() if len(fields) >= 3 and fields[2].strip() else None
        entries[word] = ReferenceEntry(codes=codes, weight=weight)
    return entries


def _split_concatenated_pinyin(value: str, length: int) -> list[str] | None:
    """Best-effort split for ``nihao`` when a source omitted spaces."""

    normalized = value.lower().replace("'", "")
    syllables = set(PINYIN_FINALS)
    syllables.update(
        initial + final
        for initial in PINYIN_INITIALS
        for final in PINYIN_FINALS
    )
    # Dynamic programming prefers the longest valid syllable at each position.
    memo: dict[tuple[int, int], list[str] | None] = {}

    def visit(position: int, count: int) -> list[str] | None:
        key = (position, count)
        if key in memo:
            return memo[key]
        if count == length:
            answer = [] if position == len(normalized) else None
            memo[key] = answer
            return answer
        for end in range(min(len(normalized), position + 6), position, -1):
            candidate = normalized[position:end]
            if candidate not in syllables:
                continue
            rest = visit(end, count + 1)
            if rest is not None:
                answer = [candidate, *rest]
                memo[key] = answer
                return answer
        memo[key] = None
        return None

    return visit(0, 0)


def _select_reading_for_token(
    token: str,
    readings: Sequence[Reading],
) -> tuple[str, str] | None:
    """Match a source pinyin token and return the dictionary's toned spelling.

    Hand-written dictionaries commonly omit tones.  Matching their normalized
    spelling against the character table lets the generated dictionary add
    the tone without changing an explicitly supplied auxiliary code.
    """

    pinyin, separator, aux = token.partition(";")
    normalized = strip_tone(pinyin)
    if not normalized:
        return None
    for candidate in readings:
        if strip_tone(candidate.pinyin) == normalized:
            return candidate.pinyin, aux.strip() or candidate.aux
    return None


def _explicit_code(token: str, reading: Reading | None, readings: Sequence[Reading]) -> str:
    """Normalize a source token, retaining explicit auxiliary-code overrides."""

    pinyin, separator, aux = token.partition(";")
    pinyin = pinyin.strip()
    aux = aux.strip() if separator else ""
    matched = _select_reading_for_token(token, readings)
    if matched is not None:
        toned_pinyin, matched_aux = matched
        return f"{toned_pinyin};{aux or matched_aux}" if (aux or matched_aux) else toned_pinyin
    if not aux and reading is not None:
        aux = reading.aux
    return f"{pinyin};{aux}" if aux else pinyin


def _record_ambiguous(
    stats: BuildStats,
    character: str,
    readings: Sequence[Reading],
    source_name: str,
    line_number: int,
    word: str,
    selected: Reading,
) -> None:
    if len(readings) < 2:
        return
    assert stats.ambiguous is not None
    entry = stats.ambiguous.setdefault(
        character,
        {"readings": list(readings), "selected": selected, "occurrences": []},
    )
    occurrence = f"{source_name}:{line_number} ({word})"
    if occurrence not in entry["occurrences"]:
        entry["occurrences"].append(occurrence)


def _codes_for_word(
    word: str,
    pinyin_tokens: Sequence[str],
    table: dict[str, list[Reading]],
    stats: BuildStats,
    source_name: str,
    line_number: int,
) -> tuple[str, bool] | None:
    characters = list(word)
    cjk_positions = [index for index, character in enumerate(characters) if is_cjk(character)]
    target_positions = cjk_positions or list(range(len(characters)))

    tokens = list(pinyin_tokens)
    if tokens and len(tokens) == 1 and len(target_positions) > 1:
        split = _split_concatenated_pinyin(tokens[0], len(target_positions))
        if split:
            tokens = split

    if tokens and len(tokens) != len(target_positions):
        # A malformed source line should not prevent the rest of the file from
        # building.  Use supplied tokens where possible, then fill the gaps.
        stats.malformed_entries += 1
    explicit = bool(tokens)
    if explicit:
        stats.explicit_pinyin_entries += 1
    else:
        stats.inferred_pinyin_entries += 1

    token_by_position = {
        position: tokens[index]
        for index, position in enumerate(target_positions)
        if index < len(tokens)
    }
    codes: list[str] = []
    for position, character in enumerate(characters):
        if position not in target_positions:
            # Keep separators/Latin fragments usable in mixed-language entries.
            if not character.isspace():
                codes.append(character.lower())
            continue
        readings = table.get(character, [])
        selected = readings[0] if readings else None
        if len(readings) > 1 and selected is not None:
            _record_ambiguous(stats, character, readings, source_name, line_number, word, selected)
        token = token_by_position.get(position)
        if token:
            codes.append(_explicit_code(token, selected, readings))
        elif selected is not None:
            codes.append(f"{selected.pinyin};{selected.aux}" if selected.aux else selected.pinyin)
        elif character.isascii() and not character.isspace():
            codes.append(character.lower())
        else:
            assert stats.unknown_chars is not None
            stats.unknown_chars[character] += 1
            return None
    return " ".join(codes), explicit


def _header_for_source(path: Path, encoding: str = "utf-8") -> list[str]:
    header: list[str] = []
    try:
        with path.open("r", encoding=encoding, errors="replace") as handle:
            for raw in handle:
                line = raw.rstrip("\r\n")
                header.append(line)
                if line.strip() == "...":
                    return header
    except OSError:
        pass
    name = path.stem
    if name.endswith(".dict"):
        name = name[:-5]
    return ["---", f"name: {name}", 'version: "1.0"', "sort: by_weight", "..."]


def _write_output_header(handle, source: Path, encoding: str) -> None:
    for line in _header_for_source(source, encoding):
        handle.write(line + "\n")


def build_file(
    source: Path,
    destination: Path,
    table: dict[str, list[Reading]],
    stats: BuildStats,
    encoding: str = "utf-8",
    reference: dict[str, ReferenceEntry] | None = None,
) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "w", encoding=encoding, newline="\n", dir=destination.parent,
            prefix=destination.name + ".", suffix=".tmp", delete=False,
        ) as handle:
            temporary = Path(handle.name)
            _write_output_header(handle, source, encoding)
            for line_number, line in _iter_data_lines(source, encoding):
                stats.source_entries += 1
                parsed = _parse_source_line(line)
                if parsed is None:
                    stats.skipped_entries += 1
                    continue
                word, pinyin_tokens, weight = parsed
                reviewed = reference.get(word) if reference is not None else None
                if reviewed is not None:
                    # Keep the current source weight when present, but retain
                    # a reviewed entry's weight if the source omits one.
                    output_weight = weight if weight is not None else reviewed.weight
                    handle.write(word + "\t" + reviewed.codes)
                    if output_weight is not None:
                        handle.write("\t" + output_weight)
                    handle.write("\n")
                    stats.referenced_entries += 1
                    stats.written_entries += 1
                    continue
                converted = _codes_for_word(
                    word, pinyin_tokens, table, stats, source.name, line_number
                )
                if converted is None:
                    stats.skipped_entries += 1
                    continue
                codes, _ = converted
                handle.write(word + "\t" + codes)
                if weight is not None:
                    handle.write("\t" + weight)
                handle.write("\n")
                stats.written_entries += 1
        os.replace(temporary, destination)
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink(missing_ok=True)


def _markdown_cell(value: str) -> str:
    return value.replace("|", "\\|").replace("\n", " ")


def write_report(
    report_path: Path,
    stats: BuildStats,
    table: dict[str, list[Reading]],
    source_dir: Path,
    output_dir: Path,
    character_dict: Path,
    reference_entries: int = 0,
) -> None:
    report_path.parent.mkdir(parents=True, exist_ok=True)
    ambiguous = stats.ambiguous or {}
    unknown = stats.unknown_chars or Counter()
    with report_path.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write("# build_dict report\n\n")
        handle.write(f"Generated: {datetime.now().astimezone().isoformat(timespec='seconds')}\n\n")
        handle.write("## Summary\n\n")
        handle.write(f"- Source directory: `{source_dir}`\n")
        handle.write(f"- Output directory: `{output_dir}`\n")
        handle.write(f"- Character dictionary: `{character_dict}`\n")
        handle.write(f"- Reviewed entries reused: {reference_entries}\n")
        handle.write(f"- Character entries loaded: {len(table)}\n")
        handle.write(f"- Source files: {stats.source_files}\n")
        handle.write(f"- Source entries: {stats.source_entries}\n")
        handle.write(f"- Written entries: {stats.written_entries}\n")
        handle.write(f"- Skipped entries: {stats.skipped_entries}\n")
        handle.write(f"- Malformed pinyin fields: {stats.malformed_entries}\n")
        handle.write(f"- Lines with explicit pinyin: {stats.explicit_pinyin_entries}\n")
        handle.write(f"- Lines with inferred pinyin: {stats.inferred_pinyin_entries}\n")
        handle.write(f"- Polyphonic characters to review: {len(ambiguous)}\n")
        handle.write("\n")

        handle.write("## Polyphonic characters to review\n\n")
        handle.write(
            "The builder selected the highest-weight reading. Review these characters "
            "in context; source occurrences are listed in the last column.\n\n"
        )
        if ambiguous:
            handle.write("| Character | Selected | All readings (weight) | Source occurrences |\n")
            handle.write("| --- | --- | --- | --- |\n")
            for character in sorted(ambiguous):
                item = ambiguous[character]
                readings = "; ".join(reading.label for reading in item["readings"])
                occurrences = "; ".join(item["occurrences"])
                handle.write(
                    f"| {_markdown_cell(character)} | {_markdown_cell(item['selected'].label)} | "
                    f"{_markdown_cell(readings)} | {_markdown_cell(occurrences)} |\n"
                )
        else:
            handle.write("No polyphonic characters were encountered.\n")
        handle.write("\n")

        handle.write("## Unresolved characters\n\n")
        if unknown:
            handle.write("These entries were skipped because no character reading was available:\n\n")
            for character, count in sorted(unknown.items()):
                handle.write(f"- `{character}` (U+{ord(character):04X}), {count} occurrence(s)\n")
        else:
            handle.write("None.\n")


def build(
    input_dir: Path = DEFAULT_INPUT_DIR,
    output_dir: Path = DEFAULT_OUTPUT_DIR,
    character_dict: Path = DEFAULT_ZI_DICT,
    cache_path: Path | None = DEFAULT_CACHE,
    report_path: Path = DEFAULT_REPORT,
    use_reference: bool = True,
    use_cache: bool = True,
    encoding: str = "utf-8",
) -> BuildStats:
    sources = sorted(Path(input_dir).glob("*.yaml"))
    if not sources:
        raise FileNotFoundError(f"no .yaml dictionary files found in {input_dir}")
    table = load_character_dictionary(character_dict, cache_path, use_cache, encoding)
    stats = BuildStats(source_files=len(sources))
    for source in sources:
        destination = Path(output_dir) / source.name
        reference = _load_reference_dictionary(destination, encoding) if use_reference else None
        build_file(source, destination, table, stats, encoding, reference)
    write_report(
        report_path,
        stats,
        table,
        Path(input_dir),
        Path(output_dir),
        Path(character_dict),
        stats.referenced_entries,
    )
    return stats


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build Rime dictionaries from dict/*.yaml")
    parser.add_argument("--input-dir", type=Path, default=DEFAULT_INPUT_DIR)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--zi-dict", type=Path, default=DEFAULT_ZI_DICT)
    parser.add_argument("--cache", type=Path, default=DEFAULT_CACHE, help="character-table cache path")
    parser.add_argument("--no-cache", action="store_true", help="rescan the character dictionary")
    parser.add_argument(
        "--no-reference",
        action="store_true",
        help="ignore existing output entries and rebuild every word",
    )
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    parser.add_argument("--encoding", default="utf-8")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        stats = build(
            input_dir=args.input_dir,
            output_dir=args.output_dir,
            character_dict=args.zi_dict,
            cache_path=args.cache,
            report_path=args.report,
            use_reference=not args.no_reference,
            use_cache=not args.no_cache,
            encoding=args.encoding,
        )
    except (FileNotFoundError, OSError, UnicodeError) as error:
        print(f"build_dict: {error}", file=sys.stderr)
        return 2
    print(
        f"Built {stats.written_entries} entries from {stats.source_files} file(s); "
        f"reused {stats.referenced_entries}, skipped {stats.skipped_entries}. "
        f"Report: {args.report}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
