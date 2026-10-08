"""Convert mail HTML to plain text without rendering or fetching remote content."""

import re
from html.parser import HTMLParser


class MessageTextParser(HTMLParser):
    _hidden = {"script", "style", "template", "head"}
    _blocks = {"address", "article", "blockquote", "br", "div", "h1", "h2", "h3", "h4", "h5", "h6", "hr", "li", "p", "pre", "tr"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.hidden: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in self._hidden:
            self.hidden.append(tag)
        elif not self.hidden and tag in self._blocks:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in self.hidden:
            del self.hidden[self.hidden.index(tag) :]
        elif not self.hidden and tag in self._blocks:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self.hidden:
            self.parts.append(data)

    def text(self) -> str:
        lines = [re.sub(r"[^\S\n]+", " ", line).strip() for line in "".join(self.parts).splitlines()]
        return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def html_to_text(value: str) -> str:
    parser = MessageTextParser()
    parser.feed(value)
    parser.close()
    return parser.text()
