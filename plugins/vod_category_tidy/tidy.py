"""Category renaming and merging, independent of Dispatcharr so it can be tested on its own.

A merged category takes the lowest id of its members, so its id stays stable across refreshes
as long as that provider category exists.
"""
import re


def parse_rules(text):
    """Lines of "Display name = Provider A, Provider B" -> {provider name (lowercase): display}.
    Blank lines and lines starting with # are ignored."""
    rules = {}
    for line in (text or "").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        display, _, members = line.partition("=")
        display = display.strip()
        for member in members.split(","):
            if member.strip() and display:
                rules[member.strip().lower()] = display
    return rules


def hide_pattern(words):
    """A regex matching any of the comma-separated words or phrases as whole words, or None."""
    parts = [w.strip() for w in (words or "").split(",") if w.strip()]
    if not parts:
        return None
    return re.compile(r"(?<!\w)(" + "|".join(re.escape(p) for p in parts) + r")(?!\w)", re.IGNORECASE)


class Tidy:
    """Maps one category type's provider categories to display categories.

    categories: [(id, provider name)] for one type (movie or series). hide_words: a comma-
    separated list; a category whose provider or shown name contains one is hidden, titles and all.
    """

    def __init__(self, categories, rules, strip_pattern, hide_words=""):
        strip = re.compile(strip_pattern, re.IGNORECASE) if strip_pattern else None
        hide = hide_pattern(hide_words)
        self.display, self.hidden = {}, set()
        for cid, name in categories:
            shown = rules.get(name.lower())
            if shown is None:
                shown = (strip.sub("", name).strip() if strip else name) or name
            if hide and (hide.search(name) or hide.search(shown)):
                self.hidden.add(cid)
                continue
            self.display[cid] = shown
        self.members = {}
        for cid, shown in self.display.items():
            self.members.setdefault(shown, []).append(cid)
        for ids in self.members.values():
            ids.sort()

    def canonical(self, cid):
        """The id apps see for a provider category: the lowest id in its display category."""
        shown = self.display.get(cid)
        return self.members[shown][0] if shown is not None else cid

    def expand(self, cid):
        """All provider categories behind the id an app asked for (none if it's hidden)."""
        if cid in self.hidden:
            return []
        shown = self.display.get(cid)
        if shown is None:
            return [cid]
        members = self.members[shown]
        return members if members[0] == cid else [cid]

    def categories(self, provider_rows):
        """Rewrite an XC category list: one entry per display category, sorted by name."""
        merged = {}
        for row in provider_rows:
            cid = int(row["category_id"])
            if cid in self.hidden:
                continue
            shown = self.display.get(cid, row["category_name"])
            merged.setdefault(shown, {**row, "category_id": str(self.canonical(cid)), "category_name": shown})
        return sorted(merged.values(), key=lambda r: r["category_name"].lower())

    def describe(self, categories):
        """Preview lines: each shown category with the provider ones behind it, then the hidden.
        categories is the same [(id, provider name)] the Tidy was built from."""
        lines = []
        for shown in sorted(self.members, key=str.lower):
            names = sorted(name for cid, name in categories if self.display.get(cid) == shown)
            lines.append(f"  {shown}" + (f"  <- {', '.join(names)}" if names != [shown] else ""))
        hidden = sorted(name for cid, name in categories if cid in self.hidden)
        if hidden:
            lines.append(f"  hidden: {', '.join(hidden)}")
        return lines

    def by_category(self, items):
        """Order a full listing by shown category, then title, so apps that build their category
        list from the order titles arrive in still show categories A-Z."""
        def key(item):
            cid = item.get("category_id")
            shown = self.display.get(int(cid)) if cid and cid != "0" else None
            return (shown is None, (shown or "").lower(), (item.get("name") or "").lower())

        items.sort(key=key)
        for num, item in enumerate(items, 1):
            item["num"] = num
        return items

    def relabel(self, items):
        """Point each movie or series at its display category's id, dropping hidden ones."""
        kept = []
        for item in items:
            cid = item.get("category_id")
            if cid and cid != "0":
                if int(cid) in self.hidden:
                    continue
                canon = self.canonical(int(cid))
                item["category_id"] = str(canon)
                item["category_ids"] = [canon]
            kept.append(item)
        return kept
