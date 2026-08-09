# Kauai Guide

A map-based guide to what to see, do, and eat on Kauai — powered by Yelp Fusion API and Leaflet/OpenStreetMap.

## Setup

```bash
cd kauai-guide
python3 -m venv venv
source venv/bin/activate
pip3 install -r requirements.txt
```

## Yelp API Key

1. Go to https://www.yelp.com/developers
2. Create a free app → copy your API key
3. Set it as an environment variable before running:

```bash
export YELP_API_KEY='your-key-here'
python3 app.py
```

Or on subsequent runs:
```bash
source venv/bin/activate
YELP_API_KEY='your-key-here' python3 app.py
```

Then open http://localhost:5051

## Features

- Map locked to Kauai island
- **Eat / Do / See** tabs pulling live Yelp results
- Search within each category
- Click any card or marker to see details
- Upload your own photos pinned to Kauai locations
- **My Photos** tab shows your uploads on the map
