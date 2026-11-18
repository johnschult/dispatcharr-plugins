import json
from pathlib import Path

from tidy import Tidy, hide_pattern, parse_rules

MANIFEST = json.loads((Path(__file__).parent.parent / "plugin.json").read_text(encoding="utf-8"))
DEFAULTS = {f["id"]: f["default"] for f in MANIFEST["fields"]}
STRIP = DEFAULTS["strip_pattern"]


# parse_rules

def test_rules_map_each_provider_name_to_its_display_name():
    assert parse_rules("Drama = Movie-Drama, Series-Soap") == {"movie-drama": "Drama", "series-soap": "Drama"}


def test_rules_skip_blank_comment_and_malformed_lines():
    assert parse_rules("\n# Drama = Movie-Drama\nno equals sign\n = Movie-Orphan\nDrama = , Movie-Drama,\n") == {
        "movie-drama": "Drama"
    }


def test_shipped_rules_parse():
    rules = parse_rules(DEFAULTS["rules"])
    assert rules["movie-action"] == "Action & Adventure"
    assert rules["series-war & politics"] == "Documentary"


# hide_pattern

def test_hide_matches_whole_words_ignoring_case():
    p = hide_pattern("CAM, Multi-Lang")
    assert p.search("Movies cam")
    assert p.search("MULTI-LANG Series")
    assert not p.search("Camera Obscura")
    assert not p.search("Webcam Specials")


def test_hide_with_no_words_is_none():
    assert hide_pattern("") is None
    assert hide_pattern(" , ,") is None


# Tidy: display names, merging, hiding

def tidy(categories, rules="", strip=STRIP, hide=""):
    return Tidy(categories, parse_rules(rules), strip, hide)


def test_rule_wins_over_stripping():
    t = tidy([(1, "Movie-Drama")], "Big Drama = Movie-Drama")
    assert t.display == {1: "Big Drama"}


def test_rule_match_ignores_case():
    assert tidy([(1, "MOVIE-drama")], "Drama = Movie-Drama").display == {1: "Drama"}


def test_unruled_names_lose_the_prefix():
    t = tidy([(1, "Movie- Westerns"), (2, "Movies-Kids"), (3, "Series - Anime"), (4, "Documentaries")])
    assert t.display == {1: "Westerns", 2: "Kids", 3: "Anime", 4: "Documentaries"}


def test_no_strip_pattern_keeps_names():
    assert tidy([(1, "Movie-Westerns")], strip="").display == {1: "Movie-Westerns"}


def test_name_that_strips_to_nothing_keeps_its_name():
    assert tidy([(1, "Movies - ")]).display == {1: "Movies - "}


def test_merged_category_uses_its_lowest_id():
    t = tidy([(9, "Movie-Drama"), (4, "Series-Drama"), (7, "Movie-Comedy")], "Drama = Movie-Drama, Series-Drama")
    assert t.members == {"Drama": [4, 9], "Comedy": [7]}
    assert t.canonical(9) == 4
    assert t.canonical(4) == 4


def test_stripped_name_joins_a_rule_category_of_the_same_name():
    t = tidy([(3, "Movie-Drama"), (8, "Movies-Drama")], "Drama = Movie-Drama")
    assert t.members == {"Drama": [3, 8]}


def test_hidden_by_provider_name_or_shown_name():
    t = tidy([(1, "Movie-Turkish"), (2, "Movies-Old"), (3, "Movie-Action")], "Turkish Gold = Movies-Old", hide="Turkish")
    assert t.hidden == {1, 2}
    assert t.display == {3: "Action"}


def test_unknown_category_maps_to_itself():
    t = tidy([(1, "Movie-Action")])
    assert t.canonical(99) == 99
    assert t.expand(99) == [99]


# expand: what an app's category request means

def test_expand_canonical_id_to_every_member():
    t = tidy([(4, "Series-Drama"), (9, "Movie-Drama")], "Drama = Movie-Drama, Series-Drama")
    assert t.expand(4) == [4, 9]


def test_expand_non_canonical_member_to_itself():
    t = tidy([(4, "Series-Drama"), (9, "Movie-Drama")], "Drama = Movie-Drama, Series-Drama")
    assert t.expand(9) == [9]


def test_expand_hidden_to_nothing():
    assert tidy([(1, "Movie-CAM")], hide="CAM").expand(1) == []


# categories: the XC category list apps see

def row(cid, name, parent=0):
    return {"category_id": str(cid), "category_name": name, "parent_id": parent}


def test_category_list_merges_hides_and_sorts():
    t = tidy(
        [(9, "Movie-Drama"), (4, "Series-Drama"), (7, "movie-comedy"), (5, "Movie-CAM")],
        "Drama = Movie-Drama, Series-Drama",
        hide="CAM",
    )
    out = t.categories([row(9, "Movie-Drama"), row(4, "Series-Drama"), row(7, "movie-comedy"), row(5, "Movie-CAM")])
    assert out == [row(7, "comedy"), row(4, "Drama")]


def test_category_list_keeps_unknown_rows_as_they_are():
    t = tidy([(1, "Movie-Action")])
    assert t.categories([row(1, "Movie-Action"), row(42, "Brand New")]) == [row(1, "Action"), row(42, "Brand New")]


def test_category_list_keeps_other_fields_of_the_first_member():
    t = tidy([(9, "Movie-Drama"), (4, "Series-Drama")], "Drama = Movie-Drama, Series-Drama")
    out = t.categories([row(9, "Movie-Drama", parent=1), row(4, "Series-Drama", parent=2)])
    assert out == [{"category_id": "4", "category_name": "Drama", "parent_id": 1}]


# relabel and by_category: the title listings

def item(name, cid):
    return {"name": name, "category_id": None if cid is None else str(cid)}


def test_relabel_points_titles_at_the_canonical_id_and_drops_hidden():
    t = tidy([(9, "Movie-Drama"), (4, "Series-Drama"), (5, "Movie-CAM")], "Drama = Movie-Drama, Series-Drama", hide="CAM")
    out = t.relabel([item("A", 9), item("B", 5), item("C", "0"), item("D", None)])
    assert out == [
        {"name": "A", "category_id": "4", "category_ids": [4]},
        item("C", "0"),
        item("D", None),
    ]


def test_by_category_orders_by_shown_category_then_title_and_numbers():
    t = tidy([(1, "Movie-Zombies"), (2, "Movie-Action")])
    out = t.by_category([item("b", 1), item("Uncat", "0"), item("z", 2), item("A", 2)])
    assert [(i["name"], i["num"]) for i in out] == [("A", 1), ("z", 2), ("b", 3), ("Uncat", 4)]


# describe: the Preview action

def test_describe_lists_members_and_hidden():
    cats = [(9, "Movie-Drama"), (4, "Series-Drama"), (7, "Movie-Comedy"), (5, "Movie-CAM")]
    t = tidy(cats, "Drama = Movie-Drama, Series-Drama", hide="CAM")
    assert t.describe(cats) == [
        "  Comedy  <- Movie-Comedy",
        "  Drama  <- Movie-Drama, Series-Drama",
        "  hidden: Movie-CAM",
    ]


def test_describe_leaves_out_arrows_for_unchanged_names():
    cats = [(1, "Documentaries")]
    assert tidy(cats).describe(cats) == ["  Documentaries"]
