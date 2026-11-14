"""Leaves out of a rendered card what an e-ink reader cannot use: controls, icon ligatures,
script-only links, media players and hidden elements. What is drawn is unchanged."""
import re
from html.parser import HTMLParser

VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source", "track", "wbr"}
DROP = {"button", "input", "select", "textarea", "label", "audio", "video", "iframe", "canvas", "object",
        "embed", "noscript", "template", "dialog"}
ICON_CLASSES = {"material-icons", "material-symbols-outlined", "material-icons-outlined", "fa", "fas", "far",
                "glyphicon"}
HIDDEN = re.compile(r"display\s*:\s*none|visibility\s*:\s*hidden", re.I)
# A box left with nothing in it: no text, no image, no line break.
EMPTY_BOX = re.compile(r"<(div|span|p|li|ul|ol|a|small|fieldset|form|nav)\b[^>]*>\s*</\1>", re.I)


def _dropped(tag: str, attrs: dict) -> bool:
    if tag in DROP:
        return True
    if HIDDEN.search(attrs.get("style") or "") or "hidden" in attrs:
        return True
    if ICON_CLASSES & set((attrs.get("class") or "").split()):
        return True
    # A link with no target only runs a script.
    return tag == "a" and not (attrs.get("href") or "").strip() and "onclick" in attrs


class _Cleaner(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=False)
        self.out = []
        self.skip = []  # open tags of the element being left out, innermost last

    def handle_starttag(self, tag, attrs):
        if self.skip:
            if tag not in VOID:
                self.skip.append(tag)
            return
        if _dropped(tag, dict(attrs)):
            if tag not in VOID:
                self.skip.append(tag)
            return
        self.out.append(self.get_starttag_text())

    def handle_startendtag(self, tag, attrs):
        if not self.skip and not _dropped(tag, dict(attrs)):
            self.out.append(self.get_starttag_text())

    def handle_endtag(self, tag):
        if self.skip:
            if tag in self.skip:  # tolerant of unclosed inner tags
                while self.skip and self.skip.pop() != tag:
                    pass
            return
        self.out.append(f"</{tag}>")

    def _text(self, text):
        if not self.skip:
            self.out.append(text)

    handle_data = _text

    def handle_entityref(self, name):
        self._text(f"&{name};")

    def handle_charref(self, name):
        self._text(f"&#{name};")

    def handle_decl(self, decl):
        self._text(f"<!{decl}>")

    def handle_comment(self, data):
        pass


def for_device(html: str) -> str:
    cleaner = _Cleaner()
    cleaner.feed(html)
    cleaner.close()
    out = "".join(cleaner.out)
    while True:  # nested empty boxes go from the inside out
        shorter = EMPTY_BOX.sub("", out)
        if shorter == out:
            return re.sub(r"\n\s*\n+", "\n", out)
        out = shorter
