# bdh-app-restaurants

BlackDiamondHub plugin app: **Restaurants** — every place to eat in Sun Peaks
with Google ratings, open-now status, photos, a clustered map, a guest
shortlist (with "send to my phone" QR) and owner picks/notes.

## Configuration

Both are on the hub's **Settings → Restaurants** page.

| Setting | Key | Default |
|---|---|---|
| Google Places API key (secret) | `google_places_api_key` | empty (no fetching) |
| Shortlist link | `shortlist_url` | `https://blackdiamondlodge.github.io/shortlist/` |

The key needs **Places API (New)** enabled and billing on (photos are a paid SKU).
Running in the hub (not in a container), the key can also come from `BDH_RESTAURANTS_GOOGLE_PLACES_API_KEY`
or `GOOGLE_PLACES_API_KEY`, and the link from `BDH_RESTAURANTS_SHORTLIST_URL`.

## Container (hub API 2)

Since 2.0.0 the app runs in its own container: no permissions (it only reads its own settings and
secret), `network: outbound` (Google Places), `lifecycle: on_demand` with `wake_every: 3600`,
`memory_mb: 96`. Pages, photos and the tile come from the app's own SQLite, so nothing needs to stay
running: whatever wakes the app (a visit, or the hub refreshing the tile at least hourly so "N open
now" stays current) also starts the background Google refresh when places are stale, and the app is
up for 10 idle minutes, far longer than a refresh takes.

In the container the hub's environment variables are not visible to the app. Set on the hub:

- the `google_places_api_key` secret (today the hub's `GOOGLE_PLACES_API_KEY`); the hub's vault
  also picks up an older plain `google_places_api_key` setting;
- `shortlist_url` only if it differs from the default (today `BDH_RESTAURANTS_SHORTLIST_URL`).

### Moving to a container

The places, owner picks/notes and cached Google data and photos are rows in the app's tables
(photos are database blobs, there are no media files), so they move with the SDK's fixture tools:

1. On the hub: `python -m bdh_sdk.appdata export restaurants -o restaurants.json`.
2. Copy `restaurants.json` into the app's data volume, then in the container run
   `python -m bdh_sdk.runtime.manage import_app_data /data/restaurants.json --replace`
   (`--replace` drops the places the container's migrations seeded).

Skipping the import only loses owner picks/notes and hidden flags: the seeded places come back and
the next refresh refetches details and photos from Google.

The container has no Django admin; add or remove places with
`python -m bdh_sdk.runtime.manage shell` there.

CI's `image` job (from bdh-sdk's `app-ci.yml`) locks the dependencies against the hub's app
runtime, builds the app's image and smoke-tests it the way the supervisor runs it.

## Data refresh & cost

- Place details refresh at most every ~20 h, photos (3 per place) every 30 days.
- Photos are cached in the database and served with immutable cache headers —
  Google is never hit per page view.
- Refresh runs lazily in a background thread when the homepage or the app is
  viewed (1 h lock). Force a refresh with:

  ```
  python manage.py refresh_restaurants --force                    # in the hub
  python -m bdh_sdk.runtime.manage refresh_restaurants --force    # in the container
  ```

## Owner editing

Signed-in users see an owner panel in each restaurant's details:
**Owner's pick**, an **owner's note** (shown to guests, independent of the pick)
and **Hide from guests**. Places are added/removed in Django admin (they need a
Google `place_id` for data).

## Shortlist

Guests heart places (stored in the kiosk browser for the whole stay). The list
resets when a different stay begins, using the hub's `hub_stay_key` template
variable; checkout or an HA outage never clears it, and a list started before a
stay shows up is kept for that stay. "Send
list to my phone" shows a QR to the static shortlist page, with the list encoded
in the URL hash — nothing is stored server-side and it works off the lodge LAN.

On phones (≤600px) the page switches to a single-column list: quick filter
chips sit under the title, the full filter groups open in a bottom sheet, and a
floating "♥ Shortlist" button opens the shortlist as a bottom sheet with
directions links and a Share button (`navigator.share` with the same shortlist
link, falling back to the QR).

To keep that QR sparse, each place goes in the link as a 3-character code that
the page looks up in its `places.json`. The kiosk only uses a code when the
published file lists the same name and place ID; otherwise that place goes in
the long `Name~PlaceID` form, so links always work. After adding or renaming
restaurants, refresh the file in the
[shortlist repo](https://github.com/blackdiamondhub/shortlist):

    python manage.py export_shortlist_places --merge places.json > new.json
