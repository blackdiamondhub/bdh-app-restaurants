from bdh_sdk import PluginSpec, Setting, Tile
from bdh_sdk.plugins import LIFECYCLE_ON_DEMAND, NETWORK_OUTBOUND, SECRET

SHORTLIST_URL = "https://blackdiamondlodge.github.io/shortlist/"

spec = PluginSpec(
    id="restaurants",
    app="restaurants",
    urls="restaurants.urls",
    status="restaurants.homepage.status",
    tiles=(
        Tile(
            title="Restaurants",
            url_name="restaurants",
            icon="restaurants/hub/icon.svg",
            section="mountain",
            subtitle="Where to eat in Sun Peaks",
            order=40,
        ),
    ),
    settings=(
        Setting("google_places_api_key", "Google Places API key", type=SECRET,
                help="Fetches hours, ratings and photos once a day. Without it the page shows only "
                     "our own list."),
        Setting("shortlist_url", "Shortlist link", default=SHORTLIST_URL,
                help="The \"Our shortlist\" link on the page."),
    ),
    api_version=2,
    # Reads only its own settings and secret.
    permissions=(),
    # Fetches details and photos from Google Places.
    network=NETWORK_OUTBOUND,
    # Pages, photos and the tile are served from the app's own database. Whatever
    # wakes it (a visit or the hub refreshing the tile) also starts the daily
    # Google refresh when places are stale; the hourly wake keeps "open now" current.
    lifecycle=LIFECYCLE_ON_DEMAND,
    wake_every=3600,
    memory_mb=96,
)
