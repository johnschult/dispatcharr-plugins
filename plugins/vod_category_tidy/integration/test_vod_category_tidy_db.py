import pytest

from helpers import module_of, settings_for

KEY = "vod_category_tidy"
HOOKED = ("xc_get_vod_categories", "xc_get_series_categories", "xc_get_vod_streams", "xc_get_series")
RULES = "Drama = Movie-Drama, Movie-Romance"


@pytest.fixture
def tidy(manager):
    """Our settings, with the plugin's 30-second settings cache cleared."""
    settings_for(KEY, rules=RULES, hide_words="CAM")
    module_of(manager, KEY)._cache["at"] = 0.0
    return manager


@pytest.fixture
def library(transactional_db):
    """Movie categories with one movie each, from an active provider account."""
    from apps.m3u.models import M3UAccount
    from apps.vod.models import M3UMovieRelation, Movie, VODCategory

    account = M3UAccount.objects.create(name="Provider", is_active=True)
    cats = {}
    for n, name in enumerate(["Movie-Romance", "Movie-Drama", "Movie-Action", "Movie-CAM"]):
        cat = VODCategory.objects.create(name=name, category_type="movie")
        movie = Movie.objects.create(name=f"{name[6:]} Film")
        M3UMovieRelation.objects.create(m3u_account=account, movie=movie, category=cat, stream_id=str(100 + n))
        cats[name] = cat.id
    return cats


@pytest.fixture
def viewer(transactional_db):
    from apps.accounts.models import User

    return User.objects.create(username="viewer", user_level=10)


def request():
    from django.test import RequestFactory

    return RequestFactory().get("/player_api.php", HTTP_HOST="localhost")


def views():
    from apps.output import views as v

    return v


def test_all_four_xc_functions_are_wrapped(tidy):
    for name in HOOKED:
        assert hasattr(getattr(views(), name), "_vct_original"), f"{name} isn't wrapped on this Dispatcharr"


def test_categories_are_merged_renamed_and_hidden(tidy, library, viewer):
    drama = min(library["Movie-Romance"], library["Movie-Drama"])

    out = views().xc_get_vod_categories(viewer)

    assert [(c["category_id"], c["category_name"]) for c in out] == [
        (str(library["Movie-Action"]), "Action"),
        (str(drama), "Drama"),
    ]


def test_merged_category_lists_every_members_movies_under_one_id(tidy, library, viewer):
    drama = min(library["Movie-Romance"], library["Movie-Drama"])

    out = views().xc_get_vod_streams(request(), viewer, str(drama))

    assert [m["name"] for m in out] == ["Drama Film", "Romance Film"]
    assert {m["category_id"] for m in out} == {str(drama)}
    assert [m["num"] for m in out] == [1, 2]


def test_full_listing_drops_hidden_and_runs_in_category_order(tidy, library, viewer):
    out = views().xc_get_vod_streams(request(), viewer, None)

    assert [m["name"] for m in out] == ["Action Film", "Drama Film", "Romance Film"]


def test_disabled_plugin_passes_the_provider_categories_through(tidy, library, viewer):
    from apps.plugins.models import PluginConfig

    PluginConfig.objects.filter(key=KEY).update(enabled=False)
    module_of(tidy, KEY)._cache["at"] = 0.0

    names = [c["category_name"] for c in views().xc_get_vod_categories(viewer)]

    assert names == ["Movie-Action", "Movie-CAM", "Movie-Drama", "Movie-Romance"]


def test_preview_describes_the_merge(tidy, library):
    result = tidy.run_action(KEY, "preview")

    assert result["status"] == "ok", result
    message = result["message"]
    assert "Hooks: " + ", ".join(HOOKED) in message
    assert "  Drama  <- Movie-Drama, Movie-Romance" in message
    assert "  hidden: Movie-CAM" in message
