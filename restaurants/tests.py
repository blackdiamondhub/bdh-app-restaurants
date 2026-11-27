import datetime as dt
import json
from unittest.mock import MagicMock, patch

from django.contrib.auth import get_user_model
from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from django.core.cache import cache
from django.core.management import call_command
from django.test import TestCase, TransactionTestCase, override_settings, tag
from django.urls import reverse
from django.utils import timezone

from restaurants import catalog, places
from restaurants.homepage import status as homepage_status
from restaurants.models import Photo, Restaurant

UTC = dt.timezone.utc


def weekly(open_h, close_h, days=range(7), close_next_day=False):
    return {"periods": [{"open": {"day": d, "hour": open_h, "minute": 0},
                         "close": {"day": (d + 1) % 7 if close_next_day else d, "hour": close_h, "minute": 0}}
                        for d in days],
            "weekdayDescriptions": ["Monday: 8:00\u202fAM\u2009\u2013\u20095:00\u202fPM"]}


def place(**extra):
    data = {"id": "pid", "rating": 4.5, "userRatingCount": 120, "utcOffsetMinutes": -420,
            "location": {"latitude": 50.8835, "longitude": -119.8865},
            "regularOpeningHours": weekly(8, 17), "googleMapsUri": "https://maps.google.com/?cid=1"}
    data.update(extra)
    return data


def local(y, mo, d, h, mi=0):
    """A UTC instant for the given wall-clock time in Sun Peaks (UTC-7)."""
    return dt.datetime(y, mo, d, h, mi, tzinfo=dt.timezone(dt.timedelta(hours=-7))).astimezone(UTC)


# 2026-11-09 is a Monday.
class StatusTests(TestCase):
    def test_open_and_closing_time(self):
        self.assertEqual(catalog.status(place(), local(2026, 11, 9, 9, 55)), ("open", "Open · until 5pm"))

    def test_uses_place_offset_not_server_timezone(self):
        # 5:30pm in Sun Peaks; a server that thinks BC is on PST would say 4:30pm, still open.
        self.assertEqual(catalog.status(place(), local(2026, 11, 9, 17, 30))[0], "closed")

    def test_opens_later_today_tomorrow_and_weekday(self):
        self.assertEqual(catalog.status(place(), local(2026, 11, 9, 6)), ("closed", "Opens 8am"))
        self.assertEqual(catalog.status(place(), local(2026, 11, 9, 18)), ("closed", "Opens tomorrow 8am"))
        friday_only = place(regularOpeningHours=weekly(11, 21, days=[5]))
        self.assertEqual(catalog.status(friday_only, local(2026, 11, 9, 12)), ("closed", "Opens Fri 11am"))

    def test_overnight_hours(self):
        late = place(regularOpeningHours=weekly(17, 2, close_next_day=True))
        self.assertEqual(catalog.status(late, local(2026, 11, 10, 1)), ("open", "Open · until 2am"))
        # Saturday night into Sunday wraps the week.
        sat = place(regularOpeningHours=weekly(20, 0, days=[6], close_next_day=True))
        self.assertEqual(catalog.status(sat, local(2026, 11, 14, 23, 30)), ("open", "Open · until midnight"))
        self.assertEqual(catalog.status(sat, local(2026, 11, 15, 0, 30))[0], "closed")

    def test_half_hours(self):
        data = place(regularOpeningHours={"periods": [
            {"open": {"day": 1, "hour": 8, "minute": 30}, "close": {"day": 1, "hour": 14, "minute": 30}}]})
        self.assertEqual(catalog.status(data, local(2026, 11, 9, 8)), ("closed", "Opens 8:30am"))
        self.assertEqual(catalog.status(data, local(2026, 11, 9, 9)), ("open", "Open · until 2:30pm"))

    def test_always_open_seasonal_and_unknown(self):
        always = place(regularOpeningHours={"periods": [{"open": {"day": 0, "hour": 0, "minute": 0}}]})
        self.assertEqual(catalog.status(always, local(2026, 11, 9, 3)), ("open", "Open 24 hours"))
        self.assertEqual(catalog.status(place(businessStatus="CLOSED_TEMPORARILY"))[0], "season")
        self.assertEqual(catalog.status(place(businessStatus="FUTURE_OPENING")), ("season", "Closed for the season"))
        self.assertEqual(catalog.status({}), ("unknown", "Hours unavailable"))
        self.assertEqual(catalog.status(place(regularOpeningHours={}))[0], "unknown")


class CatalogTests(TestCase):
    def test_tags_combine_curation_google_and_pick(self):
        r = Restaurant(slug="x", name="X", category="cafe", tags="apres", pick=True,
                       data={"servesBrunch": True, "menuForChildren": True, "outdoorSeating": True, "reservable": False})
        self.assertEqual(catalog.tags(r), {"apres", "coffee", "breakfast", "kids", "patio", "pick"})

    def test_item(self):
        r = Restaurant(slug="x", name="X", category="pizza", place_id="pid", note="Call ahead",
                       data=place(priceLevel="PRICE_LEVEL_MODERATE", takeout=True,
                                  editorialSummary={"text": "Wood-fired pizza."}))
        i = catalog.item(r, [], local(2026, 11, 9, 12))
        self.assertEqual(i["price"], "$$")
        self.assertEqual(i["st"], ["open", "Open · until 5pm"])
        self.assertIn("open", i["tags"].split())
        self.assertEqual(i["summary"], "Wood-fired pizza.")
        self.assertEqual(i["amen"], [["fa-bag-shopping", "Takeout"]])
        self.assertTrue(i["dist"].startswith("1.6 km"))
        self.assertEqual(i["note"], "Call ahead")

    def test_place_without_google_data(self):
        r = Restaurant(slug="fondue", name="Fondue Stube", category="fondue")
        i = catalog.item(r, [])
        self.assertEqual(i["st"][0], "unknown")
        self.assertIsNone(i["loc"])
        self.assertIn("google.com/maps/search", i["maps"])


class SeedTests(TestCase):
    def test_seeded(self):
        self.assertEqual(Restaurant.objects.count(), 23)
        self.assertEqual(Restaurant.objects.exclude(place_id="").count(), 23)
        self.assertFalse(Restaurant.objects.filter(pick=True).exists())


class ShortlistExportTests(TestCase):
    def export(self, *args):
        from io import StringIO
        out = StringIO()
        call_command("export_shortlist_places", *args, stdout=out, stderr=StringIO())
        return json.loads(out.getvalue())["places"]

    def test_seeded_places_get_distinct_codes(self):
        places = self.export()
        self.assertEqual(len(places), Restaurant.objects.count())
        r = Restaurant.objects.get(slug="bolacco-cafe")
        self.assertEqual(places[catalog.short_code(r.slug)], {"n": r.name, "p": r.place_id})

    def test_merge_keeps_removed_places_and_refreshes_current(self):
        import os
        import tempfile
        r = Restaurant.objects.get(slug="bolacco-cafe")
        code = catalog.short_code(r.slug)
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as f:
            json.dump({"v": 1, "places": {"zzz": {"n": "Gone", "p": "old"}, code: {"n": "Old name", "p": "x"}}}, f)
        self.addCleanup(os.remove, f.name)
        places = self.export("--merge", f.name)
        self.assertEqual(places["zzz"], {"n": "Gone", "p": "old"})
        self.assertEqual(places[code]["n"], r.name)


def response(json_data=None, content=b"", content_type="application/json"):
    r = MagicMock()
    r.json.return_value = json_data
    r.content = content
    r.headers = {"Content-Type": content_type}
    r.raise_for_status.return_value = None
    return r


@override_settings(BDH_RESTAURANTS_GOOGLE_PLACES_API_KEY="k")
class RefreshTests(TestCase):
    def setUp(self):
        Restaurant.objects.exclude(slug__in=["bolacco-cafe", "fondue-stube"]).delete()
        Restaurant.objects.filter(slug="fondue-stube").update(place_id="")  # a place without an ID
        self.r = Restaurant.objects.get(slug="bolacco-cafe")

    def fake_get(self, url, **kwargs):
        if url.endswith("/media"):
            return response(content=b"jpeg", content_type="image/jpeg")
        return response(place(photos=[{"name": f"places/pid/photos/{n}",
                                       "authorAttributions": [{"displayName": f"A{n}"}]} for n in range(5)]))

    def test_fetches_details_and_three_photos(self):
        with patch("restaurants.places.requests.get", side_effect=self.fake_get) as get:
            counts = places.refresh()
        self.assertEqual(counts, {"details": 1, "photos": 3, "errors": 0, "skipped": False})
        self.assertEqual(get.call_count, 4)
        self.assertEqual(get.call_args_list[0].kwargs["headers"]["X-Goog-Api-Key"], "k")
        self.r.refresh_from_db()
        self.assertEqual(self.r.data["rating"], 4.5)
        self.assertEqual([p.author for p in self.r.photos.all()], ["A0", "A1", "A2"])
        self.assertEqual(bytes(self.r.photos.first().image), b"jpeg")

    def test_photos_not_refetched_with_daily_details(self):
        with patch("restaurants.places.requests.get", side_effect=self.fake_get):
            places.refresh()
        tomorrow = timezone.now() + dt.timedelta(days=1)
        with patch("restaurants.places.requests.get", side_effect=self.fake_get) as get:
            counts = places.refresh(now=tomorrow)
        self.assertEqual((counts["details"], counts["photos"]), (1, 0))
        self.assertEqual(get.call_count, 1)
        with patch("restaurants.places.requests.get", side_effect=self.fake_get) as get:
            places.refresh(now=tomorrow + dt.timedelta(hours=1))
        get.assert_not_called()
        with patch("restaurants.places.requests.get", side_effect=self.fake_get) as get:
            counts = places.refresh(now=timezone.now() + dt.timedelta(days=31))
        self.assertEqual(counts["photos"], 3)
        self.assertEqual(Photo.objects.count(), 3)

    def test_overlapping_refreshes_fetch_and_save_once(self):
        # A second refresh (page thread vs management command) starts while
        # the first is downloading photos: it must not call Google again.
        nested = []

        def get(url, **kwargs):
            if url.endswith("/media") and not nested:
                nested.append(places.refresh())
            return self.fake_get(url, **kwargs)

        with patch("restaurants.places.requests.get", side_effect=get) as mock:
            counts = places.refresh()
        self.assertEqual(nested[0], {"details": 0, "photos": 0, "errors": 0, "skipped": False})
        self.assertEqual((counts["details"], counts["photos"]), (1, 3))
        self.assertEqual(mock.call_count, 4)
        self.assertEqual(self.r.photos.count(), 3)

    def test_failure_keeps_old_data_and_waits_a_day(self):
        self.r.data = {"rating": 4.0}
        self.r.save()
        with patch("restaurants.places.requests.get", side_effect=places.requests.ConnectionError("down")):
            counts = places.refresh()
        self.assertEqual(counts["errors"], 1)
        self.r.refresh_from_db()
        self.assertEqual(self.r.data, {"rating": 4.0})
        self.assertFalse(places.needs_refresh())

    @override_settings(BDH_RESTAURANTS_GOOGLE_PLACES_API_KEY="")
    def test_no_key_no_calls(self):
        with patch("restaurants.places.requests.get") as get, patch("restaurants.places.threading.Thread") as thread:
            self.assertTrue(places.refresh()["skipped"])
            self.assertFalse(places.maybe_refresh_async())
        get.assert_not_called()
        thread.assert_not_called()

    def test_async_refresh_is_rate_limited(self):
        cache.delete(places.LOCK)
        with patch("restaurants.places.threading.Thread") as thread:
            self.assertTrue(places.maybe_refresh_async())
            self.assertFalse(places.maybe_refresh_async())
        thread.assert_called_once()
        cache.delete(places.LOCK)

    def test_management_command(self):
        with patch("restaurants.places.requests.get", side_effect=self.fake_get):
            call_command("refresh_restaurants", stdout=MagicMock())
        self.assertEqual(Photo.objects.count(), 3)


class ManifestTests(TestCase):
    def test_hub_api_2_manifest(self):
        from restaurants.plugin import spec
        m = spec.manifest()
        self.assertEqual(m["api_version"], 2)
        self.assertEqual(m["permissions"], [])
        self.assertEqual((m["network"], m["lifecycle"], m["wake_every"], m["memory_mb"]),
                         ("outbound", "on_demand", 3600, 96))
        types = {s.key: s.type for s in spec.settings}
        self.assertEqual(types, {"google_places_api_key": "secret", "shortlist_url": "str"})

    def test_default_shortlist_url_follows_the_github_org(self):
        """The shortlist is a Pages site in our org; an org rename 404'd the old default for guests."""
        import re
        from importlib.metadata import metadata

        from restaurants.plugin import SHORTLIST_URL

        urls = metadata("bdh-app-restaurants").get_all("Project-URL") or []
        source = next(u.split(",", 1)[1].strip() for u in urls if u.lower().startswith("source,"))
        org = re.match(r"https://github\.com/([^/]+)/", source).group(1)
        self.assertEqual(SHORTLIST_URL, f"https://{org.lower()}.github.io/shortlist/")


class BrokerTests(TestCase):
    """In its container the app reads its key and settings from the hub through the broker."""

    def setUp(self):
        from bdh_sdk import rpc
        from bdh_sdk.testing import FakeBroker

        self.broker = FakeBroker(grants=("settings.get", "secrets.get"), install=True, app_id="restaurants")
        self.broker.start()
        self.addCleanup(self.broker.stop)
        self.flush = lambda: rpc.flush(5)
        Restaurant.objects.exclude(slug="bolacco-cafe").delete()

    def refresh(self):
        def get(url, **kwargs):
            if url.endswith("/media"):
                return response(content=b"jpeg", content_type="image/jpeg")
            return response(place(photos=[{"name": "places/pid/photos/0"}]))

        with patch("restaurants.places.requests.get", side_effect=get) as mock:
            counts = places.refresh()
        return counts, mock

    def test_refresh_uses_the_secret_from_the_broker(self):
        self.broker.secrets[("restaurants", "google_places_api_key")] = "from-vault"
        counts, get = self.refresh()
        self.assertEqual((counts["details"], counts["photos"]), (1, 1))
        self.assertEqual(get.call_args_list[0].kwargs["headers"]["X-Goog-Api-Key"], "from-vault")
        self.assertTrue(self.broker.ops("secrets.get"))

    def test_plain_setting_is_the_fallback(self):
        self.broker.settings[("restaurants", "google_places_api_key")] = "plain"
        _, get = self.refresh()
        self.assertEqual(get.call_args_list[0].kwargs["headers"]["X-Goog-Api-Key"], "plain")

    def test_no_key_anywhere_fetches_nothing(self):
        counts, get = self.refresh()
        self.assertTrue(counts["skipped"])
        get.assert_not_called()

    def test_shortlist_url_setting(self):
        self.broker.settings[("restaurants", "shortlist_url")] = "https://example.com/list/"
        with patch("restaurants.views.maybe_refresh_async"):
            r = self.client.get(reverse("restaurants"))
        self.assertContains(r, "https://example.com/list/")


class AppDataTests(TestCase):
    """The hub's rows (photos are database blobs) survive the move to the container's SQLite."""

    def test_export_import_round_trip(self):
        from io import StringIO

        from bdh_sdk.appdata import export_rows, import_rows

        r = Restaurant.objects.get(slug="bolacco-cafe")
        r.data, r.pick, r.fetched_at = place(), True, timezone.now()
        r.save()
        Photo.objects.create(restaurant=r, position=0, image=b"\xff\xd8jpeg\x00", content_type="image/jpeg",
                             author="A")
        out = StringIO()
        count = export_rows("restaurants", out)
        self.assertEqual(count, Restaurant.objects.count() + 1)
        Restaurant.objects.filter(slug="bolacco-cafe").update(note="changed")
        Restaurant.objects.exclude(slug="bolacco-cafe").first().delete()

        self.assertEqual(import_rows("restaurants", StringIO(out.getvalue()), replace=True), count)
        self.assertEqual(Restaurant.objects.count(), 23)
        r = Restaurant.objects.get(slug="bolacco-cafe")
        self.assertEqual((r.data["rating"], r.pick, r.note), (4.5, True, ""))
        photo = r.photos.get()
        self.assertEqual((bytes(photo.image), photo.author), (b"\xff\xd8jpeg\x00", "A"))


@override_settings(BDH_RESTAURANTS_GOOGLE_PLACES_API_KEY="")
class ViewTests(TestCase):
    def setUp(self):
        self.url = reverse("restaurants")
        self.r = Restaurant.objects.get(slug="bolacco-cafe")
        self.r.data = place()
        self.r.save()
        self.photo = Photo.objects.create(restaurant=self.r, image=b"img", author="Jun")
        Restaurant.objects.filter(slug="powder-hounds").update(hidden=True, note="secret")
        self.user = get_user_model().objects.create_user("owner", password="pw", is_staff=True)
        self.member = get_user_model().objects.create_user("member", password="pw")

    def test_guest_page(self):
        r = self.client.get(self.url)
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "Bolacco Café")
        self.assertContains(r, reverse("restaurants_photo", args=[self.photo.pk]))
        self.assertNotContains(r, "Powder Hounds")
        self.assertFalse(r.context["config"]["owner"])

    def test_owner_sees_hidden(self):
        self.client.force_login(self.user)
        r = self.client.get(self.url)
        self.assertContains(r, "Powder Hounds")
        self.assertTrue(r.context["config"]["owner"])

    def test_non_admin_is_not_owner(self):
        self.client.force_login(self.member)
        r = self.client.get(self.url)
        self.assertNotContains(r, "Powder Hounds")
        self.assertFalse(r.context["config"]["owner"])
        hidden = Photo.objects.create(restaurant=Restaurant.objects.get(slug="powder-hounds"), image=b"x")
        self.assertEqual(self.client.get(reverse("restaurants_photo", args=[hidden.pk])).status_code, 404)
        url = reverse("restaurants_owner", args=["bolacco-cafe"])
        r = self.client.post(url, json.dumps({"pick": True}), content_type="application/json")
        self.assertEqual(r.status_code, 403)
        self.r.refresh_from_db()
        self.assertFalse(self.r.pick)

    def test_photo(self):
        r = self.client.get(reverse("restaurants_photo", args=[self.photo.pk]))
        self.assertEqual(r.content, b"img")
        self.assertEqual(r["Content-Type"], "image/jpeg")
        self.assertIn("immutable", r["Cache-Control"])

    def test_hidden_photo_is_owner_only(self):
        hidden = Photo.objects.create(restaurant=Restaurant.objects.get(slug="powder-hounds"), image=b"x")
        url = reverse("restaurants_photo", args=[hidden.pk])
        self.assertEqual(self.client.get(url).status_code, 404)
        self.client.force_login(self.user)
        self.assertEqual(self.client.get(url).status_code, 200)

    def test_owner_update(self):
        url = reverse("restaurants_owner", args=["bolacco-cafe"])
        body = json.dumps({"pick": True, "note": "  Best coffee  ", "hidden": False})
        self.assertEqual(self.client.post(url, body, content_type="application/json").status_code, 403)
        self.client.force_login(self.user)
        r = self.client.post(url, body, content_type="application/json")
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json()["pick"])
        self.assertIn("pick", r.json()["tags"].split())
        self.r.refresh_from_db()
        self.assertEqual((self.r.pick, self.r.note), (True, "Best coffee"))
        # The note stands on its own when the pick is removed.
        self.client.post(url, json.dumps({"pick": False}), content_type="application/json")
        self.r.refresh_from_db()
        self.assertEqual((self.r.pick, self.r.note), (False, "Best coffee"))

    def test_homepage_status(self):
        open_now = local(2026, 11, 9, 12)
        with patch("restaurants.homepage.timezone.now", return_value=open_now):
            s = homepage_status()
        self.assertEqual((s.text, s.level), ("1 open now", "on"))
        Restaurant.objects.update(data={})
        self.assertIsNone(homepage_status())


@tag("selenium")
# The shortlist URL is unreachable so links fall back to the long form.
@override_settings(BDH_RESTAURANTS_GOOGLE_PLACES_API_KEY="",
                   BDH_RESTAURANTS_SHORTLIST_URL="http://127.0.0.1:9/shortlist/")
class BrowserTests(StaticLiveServerTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        from selenium import webdriver
        options = webdriver.ChromeOptions()
        for arg in ("--headless=new", "--no-sandbox", "--disable-dev-shm-usage", "--window-size=1920,1080"):
            options.add_argument(arg)
        cls.driver = webdriver.Chrome(options=options)

    @classmethod
    def tearDownClass(cls):
        cls.driver.quit()
        super().tearDownClass()

    def test_details_shortlist_and_filters(self):
        from selenium.webdriver.common.by import By
        from selenium.webdriver.support import expected_conditions as EC
        from selenium.webdriver.support.ui import WebDriverWait

        Restaurant.objects.filter(slug="bolacco-cafe").update(data=place(), pick=True, note="Get the sandwich")
        d = self.driver
        d.get(self.live_server_url + reverse("restaurants"))
        wait = WebDriverWait(d, 10)
        d.find_element(By.CSS_SELECTOR, '.rc[data-slug="bolacco-cafe"] .hb').click()
        wait.until(EC.text_to_be_present_in_element((By.ID, "rs-list"), "Bolacco Café"))
        d.find_element(By.CSS_SELECTOR, '.rc[data-slug="bolacco-cafe"] .nm').click()
        wait.until(EC.visibility_of_element_located((By.ID, "rs-dlg")))
        self.assertIn("Get the sandwich", d.find_element(By.CSS_SELECTOR, "#rs-dlg .note").text)
        # The mini-map expands over the dialog and collapses back to the details.
        wait.until(EC.element_to_be_clickable((By.CSS_SELECTOR, "#rs-dlg .minw"))).click()
        wait.until(EC.visibility_of_element_located((By.CSS_SELECTOR, "#rs-dlg .mxw .leaflet-control-zoom-in")))
        d.find_element(By.CSS_SELECTOR, "#rs-dlg .mxc").click()
        self.assertFalse(d.find_elements(By.CSS_SELECTOR, "#rs-dlg .mxw"))
        d.find_element(By.CSS_SELECTOR, "#rs-dlg .dclose").click()
        d.find_element(By.CSS_SELECTOR, '.chip[data-f="pick"]').click()
        shown = [e.get_attribute("data-slug") for e in d.find_elements(By.CSS_SELECTOR, ".rc:not(.hide)")]
        self.assertEqual(shown, ["bolacco-cafe"])
        d.find_element(By.ID, "rs-send").click()
        wait.until(EC.visibility_of_element_located((By.CSS_SELECTOR, "#rs-sqr svg")))
        # Phone cameras percent-encode characters outside RFC 3986 (e.g. "|"),
        # which the shortlist page has to undo, so keep the link to safe ones.
        url = d.find_element(By.ID, "rs-sqr").get_attribute("data-url")
        self.assertRegex(url, r"^http://127\.0\.0\.1:9/shortlist/#[A-Za-z0-9,~%._-]+$")
        self.assertIn("Bolacco%20Caf%C3%A9~", url)


class HubFrameLayoutTests(TestCase):
    """The app renders inside the hub's iframe, already below the hub navbar, so its
    CSS must not reserve space for that navbar (top:62px / 100vh-62px / phone 118px)
    nor scope rules under the hub's `.navbar`, which doesn't exist in the iframe."""

    def test_no_hub_navbar_offsets(self):
        import pathlib
        import re
        root = pathlib.Path(__file__).resolve().parent
        self.assertTrue((root / "templates").is_dir(), root)
        self.assertTrue((root / "static").is_dir(), root)
        pattern = re.compile(r"top\s*:\s*(6[0-9]|118)px|100vh\s*-\s*(6[0-9]|118)px|\.navbar(?![-\w])")
        hits = []
        for path in list(root.glob("templates/**/*.html")) + list(root.glob("static/**/*.css")):
            for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                if pattern.search(line):
                    hits.append(f"{path.relative_to(root)}:{n}: {line.strip()}")
        self.assertEqual(hits, [])


class AssistantToolTests(TransactionTestCase):
    """The assistant's restaurant lookup, run the way the hub runs it (FakeBroker sessions).

    The fake broker runs tools on its own thread, so the rows must be committed.
    """

    def setUp(self):
        from bdh_sdk import Stay, settings
        from bdh_sdk.testing import FakeBroker

        saved = settings._store
        self.broker = FakeBroker(grants=("tools.*",), install=True, app_id="restaurants")
        self.broker.start()
        self.addCleanup(settings.set_store, saved)
        self.addCleanup(self.broker.stop)
        self.broker.stay = Stay(guest="Smith")  # a voice session during a stay is in guest mode
        Restaurant.objects.all().delete()
        fetched = local(2026, 11, 9, 6)
        for slug, name, category, tags, data in (
            ("masas", "Masa's Bar & Grill", "pub", "apres lunch dinner",
             place(regularOpeningHours=weekly(9, 22), nationalPhoneNumber="(250) 578-5434", takeout=True,
                   rating=4.3, priceLevel="PRICE_LEVEL_MODERATE")),
            ("pizza", "Mountain High Pizza", "pizza", "lunch dinner",
             place(regularOpeningHours=weekly(11, 22), delivery=True, rating=4.6)),
            ("bottoms", "Bottoms Bar & Grill", "pub", "apres dinner",
             place(regularOpeningHours=weekly(11, 1, close_next_day=True), rating=4.0)),
            ("oya", "Oya Japanese Cuisine", "japanese", "dinner",
             place(regularOpeningHours=weekly(16, 21, days=range(1, 7)), rating=4.8)),
            ("soleil", "Café Soleil", "cafe", "coffee breakfast", place(regularOpeningHours=weekly(7, 16))),
            ("fondue", "Fondue Stube", "fondue", "dinner", {"businessStatus": "FUTURE_OPENING"}),
        ):
            Restaurant.objects.create(slug=slug, name=name, category=category, tags=tags, data=data,
                                      fetched_at=fetched)
        self.now = local(2026, 11, 9, 12)  # Monday noon at the lodge
        for target, kwargs in (("restaurants.assistant.utc_now", {"side_effect": lambda: self.now}),
                               ("restaurants.places.maybe_refresh_async", {})):
            patcher = patch(target, **kwargs)
            patcher.start()
            self.addCleanup(patcher.stop)

    def call(self, args=None):
        from bdh_sdk import tools as sdk_tools
        from restaurants import assistant

        self.broker.assistant_tools.clear()
        for tool in assistant.tools(mode="guest"):
            self.broker.add_tool(tool, lambda a, t=tool.id: assistant.run_tool(t, a, mode="guest"))
        return sdk_tools.call_tool(sdk_tools.open_session(), "find_restaurants", args)

    def find(self, **args):
        result = self.call(args)
        self.assertTrue(result["ok"], result)
        return result["result"]

    def test_every_mode_gets_one_readonly_lookup(self):
        from bdh_sdk import ALL_MODES, hooks
        from restaurants import assistant
        from restaurants.plugin import spec
        for mode in sorted(ALL_MODES):
            with self.subTest(mode=mode):
                tools = assistant.tools(mode=mode)
                self.assertEqual([t.id for t in tools], ["find_restaurants"])
                self.assertEqual((tools[0].modes, tools[0].readonly, tools[0].confirm), (ALL_MODES, True, False))
                self.assertLessEqual(len(tools[0].description), 300)
        self.assertEqual([t["id"] for t in hooks.run(spec, "tools", mode="guest")], ["find_restaurants"])

    def test_one_place_in_loose_words(self):
        expected = {"name": "Masa's Bar & Grill", "kind": "Pub & grill", "status": "open until 10 pm",
                    "hours_today": "9 am to 10 pm", "phone": "(250) 578-5434", "price": "$$",
                    "rating": "4.3 stars", "offers": "takeout", "distance": "1.6 km from the lodge"}
        for text in ("Masa's", "masas", "is massas open", "Masa's bar and grill"):
            with self.subTest(text=text):
                self.assertEqual(self.find(name=text), expected)
        self.assertEqual(self.find(name="cafe soleil")["status"], "open until 4 pm")

    def test_hours_across_midnight_and_closing_days(self):
        self.now = local(2026, 11, 10, 0, 30)  # just after midnight, Monday night
        bottoms = self.find(name="bottoms")
        self.assertEqual((bottoms["status"], bottoms["hours_today"]), ("open until 1 am", "11 am to 1 am"))
        self.now = local(2026, 11, 15, 12)  # Oya is closed on Sundays
        oya = self.find(name="oya")
        self.assertEqual((oya["status"], oya["hours_today"]), ("closed, opens tomorrow at 4 pm", "closed"))
        self.assertEqual(self.find(name="oya", when="saturday")["hours_saturday"], "4 pm to 9 pm")

    def test_seasonal_closure(self):
        self.assertEqual(self.find(name="fondue stube"), {"name": "Fondue Stube", "kind": "Fondue",
                                                          "status": "closed for the season",
                                                          "hours_today": "closed for the season"})
        out = self.find(food="fondue")
        self.assertEqual((out["places"], out["closed_for_season"]), ([], "Fondue Stube"))
        self.assertNotIn("Fondue Stube", str(self.find(food="dinner")["places"]))

    def test_food_lists_open_places_first(self):
        self.now = local(2026, 11, 9, 23)
        out = self.find(food="pizza")
        self.assertEqual(out["places"], [{"name": "Mountain High Pizza", "kind": "Pizza",
                                          "status": "closed, opens tomorrow at 11 am",
                                          "hours": "today 11 am to 10 pm"}])
        self.assertEqual(self.find(food="sushi")["places"][0]["name"], "Oya Japanese Cuisine")
        self.assertEqual([p["name"] for p in self.find(food="pizza delivery")["places"]], ["Mountain High Pizza"])
        self.assertEqual([p["name"] for p in self.find(food="takeout")["places"]], ["Masa's Bar & Grill"])

    def test_dinner_in_the_afternoon_means_tonight(self):
        out = self.find(food="dinner")
        self.assertEqual(out["when"], "tonight at 6:30 pm")
        self.assertEqual([p["name"] for p in out["places"]],
                         ["Oya Japanese Cuisine", "Mountain High Pizza", "Masa's Bar & Grill", "Bottoms Bar & Grill"])
        self.assertEqual(out["places"][0]["status"], "closed, opens at 4 pm")

    def test_when_lists_only_places_open_then(self):
        self.now = local(2026, 11, 9, 23, 30)
        out = self.find()
        self.assertEqual((out["when"], out["open_then"]), ("now", "1 of 5 places"))
        self.assertEqual(out["places"][0]["status"], "open until 1 am")
        out = self.find(food="coffee", when="now")
        self.assertEqual(out["note"], "Nothing that fits is open now.")
        self.assertEqual(out["opens_next"], ["Café Soleil opens tomorrow at 7 am"])
        out = self.find(when="tomorrow at 8")
        self.assertEqual(out["when"], "tomorrow at 8 pm")

    def test_parse_when(self):
        import datetime
        from restaurants.assistant import parse_when
        noon = datetime.datetime(2026, 11, 9, 12)
        cases = {
            "7 pm": (datetime.datetime(2026, 11, 9, 19), "tonight at 7 pm"),
            "at 7": (datetime.datetime(2026, 11, 9, 19), "tonight at 7 pm"),
            "7:30 a.m.": (datetime.datetime(2026, 11, 10, 7, 30), "tomorrow at 7:30 am"),
            "tomorrow morning": (datetime.datetime(2026, 11, 10, 9), "tomorrow at 9 am"),
            "saturday dinner": (datetime.datetime(2026, 11, 14, 18, 30), "Saturday at 6:30 pm"),
            "tonight at 9": (datetime.datetime(2026, 11, 9, 21), "tonight at 9 pm"),
            "lunch": (noon, "now"),
            "breakfast": (datetime.datetime(2026, 11, 10, 8), "tomorrow at 8 am"),
        }
        for text, (when, label) in cases.items():
            with self.subTest(text=text):
                t, _, got = parse_when(text, noon)
                self.assertEqual((t, got), (when, label))
        self.assertIsNone(parse_when("right now", noon))
        self.assertEqual(parse_when("friday", noon), (None, datetime.date(2026, 11, 13), "Friday"))

    def test_unknowns_say_why(self):
        for args, error in (
            ({"name": "the bento box"}, "There's no 'the bento box' on the Sun Peaks restaurant list."),
            ({"food": "tacos"}, "None of the Sun Peaks places list taco. Try pizza"),
            ({"when": "whenever"}, "I can't tell when 'whenever' is"),
        ):
            with self.subTest(args=args):
                result = self.call(args)
                self.assertFalse(result["ok"])
                self.assertTrue(result["error"].startswith(error), result["error"])

    def test_no_hours_and_stale_hours(self):
        self.now = local(2026, 11, 16, 12)
        self.assertEqual(self.find(name="masas")["note"],
                         "Hours were last checked 7 days ago and may have changed.")
        Restaurant.objects.update(data={})
        self.assertEqual(self.call({"food": "pizza"}), {"ok": False,
                                                        "error": "I don't have the restaurants' hours right now."})