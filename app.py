import os
import uuid
import json
import time
import requests
from datetime import datetime
from flask import Flask, render_template, request, jsonify, url_for, send_from_directory
import sqlite3
import cloudinary
import cloudinary.uploader

app = Flask(__name__)
app.config['UPLOAD_FOLDER'] = os.path.join(os.path.dirname(__file__), 'uploads')
app.config['MAX_CONTENT_LENGTH'] = 64 * 1024 * 1024
ALLOWED_EXTENSIONS = {'png', 'jpg', 'jpeg', 'gif', 'webp', 'heic', 'heif'}
DB_PATH = os.path.join(os.path.dirname(__file__), 'kauai.db')
CACHE_TTL = 60 * 60 * 24  # 24 hours in seconds

cloudinary.config(
    cloud_name=os.environ.get('CLOUDINARY_CLOUD_NAME'),
    api_key=os.environ.get('CLOUDINARY_API_KEY'),
    api_secret=os.environ.get('CLOUDINARY_API_SECRET'),
    secure=True
)

KAUAI = {'lat': 22.0964, 'lng': -159.5261, 'sw_lat': 21.87, 'sw_lng': -159.85, 'ne_lat': 22.25, 'ne_lng': -159.25}

QUERIES = {
    'eat': '[out:json][timeout:25];(node["amenity"~"restaurant|cafe|bar|fast_food|pub"](21.87,-159.85,22.25,-159.25);way["amenity"~"restaurant|cafe|bar|fast_food|pub"](21.87,-159.85,22.25,-159.25););out center 60;',
    'do':  '[out:json][timeout:25];(node["tourism"~"attraction|activity|zoo|aquarium"](21.87,-159.85,22.25,-159.25);node["leisure"~"golf_course|sports_centre|water_park"](21.87,-159.85,22.25,-159.25);way["tourism"="attraction"](21.87,-159.85,22.25,-159.25););out center 60;',
    'see': '[out:json][timeout:25];(node["tourism"~"viewpoint|museum|artwork|gallery"](21.87,-159.85,22.25,-159.25);node["natural"~"beach|waterfall|bay|peak"](21.87,-159.85,22.25,-159.25);node["historic"](21.87,-159.85,22.25,-159.25);way["natural"~"beach|waterfall|bay"](21.87,-159.85,22.25,-159.25););out center 60;',
}


def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with get_db() as conn:
        conn.executescript('''
            CREATE TABLE IF NOT EXISTS photos (
                id TEXT PRIMARY KEY, filename TEXT,
                photo_url TEXT,
                title TEXT, description TEXT, category TEXT,
                lat REAL NOT NULL, lng REAL NOT NULL,
                address TEXT, source TEXT, uploaded_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS poi_cache (
                tab         TEXT PRIMARY KEY,
                data        TEXT NOT NULL,
                cached_at   REAL NOT NULL
            );
        ''')
        # Migration safety: add photo_url column if this DB predates it
        cols = [row['name'] for row in conn.execute('PRAGMA table_info(photos)').fetchall()]
        if 'photo_url' not in cols:
            conn.execute('ALTER TABLE photos ADD COLUMN photo_url TEXT')
    print("DB initialized.")


def allowed_file(f):
    return '.' in f and f.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS


def get_cached_pois(tab):
    with get_db() as conn:
        row = conn.execute(
            'SELECT data, cached_at FROM poi_cache WHERE tab = ?', (tab,)
        ).fetchone()
    if row and (time.time() - row['cached_at']) < CACHE_TTL:
        return json.loads(row['data'])
    return None


def set_cached_pois(tab, pois):
    with get_db() as conn:
        conn.execute(
            'INSERT OR REPLACE INTO poi_cache (tab, data, cached_at) VALUES (?, ?, ?)',
            (tab, json.dumps(pois), time.time())
        )


def dms_to_decimal(dms, ref):
    try:
        def to_float(val):
            if isinstance(val, tuple):
                return val[0] / val[1] if val[1] else 0
            return float(val)
        d = to_float(dms[0])
        m = to_float(dms[1])
        s = to_float(dms[2])
        decimal = d + m / 60 + s / 3600
        if ref in ('S', 'W'):
            decimal = -decimal
        return decimal
    except Exception:
        return None


def extract_exif_gps(filepath):
    try:
        from pillow_heif import register_heif_opener
        register_heif_opener()
    except ImportError:
        pass
    try:
        from PIL import Image
        img = Image.open(filepath)
        exif_data = img.getexif()
        if not exif_data:
            return None, None
        gps_ifd = exif_data.get_ifd(0x8825)
        if not gps_ifd:
            return None, None
        lat = dms_to_decimal(gps_ifd.get(2), gps_ifd.get(1, 'N'))
        lng = dms_to_decimal(gps_ifd.get(4), gps_ifd.get(3, 'E'))
        return lat, lng
    except Exception:
        return None, None


def parse_osm(elements):
    results = []
    for el in elements:
        tags = el.get('tags', {})
        name = tags.get('name') or tags.get('name:en')
        if not name:
            continue
        lat = el.get('lat') if el['type'] == 'node' else el.get('center', {}).get('lat')
        lng = el.get('lon') if el['type'] == 'node' else el.get('center', {}).get('lon')
        if not lat or not lng:
            continue
        kind = (tags.get('amenity') or tags.get('tourism') or tags.get('leisure') or
                tags.get('natural') or tags.get('historic') or '').replace('_', ' ').title()
        results.append({
            'id': str(el.get('id')), 'name': name, 'lat': lat, 'lng': lng,
            'kind': kind,
            'cuisine': tags.get('cuisine', '').replace(';', ', ').replace('_', ' ').title(),
            'address': ', '.join(filter(None, [tags.get('addr:housenumber',''), tags.get('addr:street',''), tags.get('addr:city','')])),
            'phone': tags.get('phone', ''), 'website': tags.get('website', ''),
        })
    return results


def fetch_from_overpass(tab):
    query = QUERIES.get(tab, QUERIES['eat'])
    for attempt in range(2):
        try:
            r = requests.post(
                'https://overpass-api.de/api/interpreter',
                data={'data': query},
                headers={'Content-Type': 'application/x-www-form-urlencoded', 'User-Agent': 'KauaiGuide/1.0'},
                timeout=30
            )
            r.raise_for_status()
            data = r.json()
            return parse_osm(data.get('elements', [])), None
        except requests.exceptions.Timeout:
            if attempt == 0:
                time.sleep(2)
                continue
            return None, 'Overpass API timed out. Try again in a moment.'
        except Exception as e:
            return None, str(e)
    return None, 'Failed after retry.'


def save_to_cloudinary(local_filepath):
    """Upload a local file to Cloudinary and return its permanent secure URL."""
    result = cloudinary.uploader.upload(
        local_filepath,
        folder='kauai-guide',
        resource_type='image'
    )
    return result['secure_url']


# ── Routes ─────────────────────────────────────────────────────────────────────

@app.route('/')
def index():
    return render_template('index.html', kauai=KAUAI)


@app.route('/upload', methods=['GET', 'POST'])
def upload():
    if request.method == 'POST':
        file = request.files.get('photo')
        if not file or not allowed_file(file.filename):
            return jsonify({'error': 'Invalid file type'}), 400
        ext = file.filename.rsplit('.', 1)[1].lower()
        temp_filename = f"{uuid.uuid4().hex}.{ext}"
        os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)
        temp_filepath = os.path.join(app.config['UPLOAD_FOLDER'], temp_filename)
        file.save(temp_filepath)

        # Convert HEIC to JPEG (Cloudinary can handle HEIC, but this keeps EXIF/GPS extraction reliable)
        if ext in ('heic', 'heif'):
            try:
                from pillow_heif import register_heif_opener
                register_heif_opener()
                from PIL import Image
                img = Image.open(temp_filepath)
                jpg_filename = temp_filename.rsplit('.', 1)[0] + '.jpg'
                jpg_filepath = os.path.join(app.config['UPLOAD_FOLDER'], jpg_filename)
                img.save(jpg_filepath, 'JPEG', quality=85)
                os.remove(temp_filepath)
                temp_filepath = jpg_filepath
            except Exception:
                pass

        try:
            lat = float(request.form['lat'])
            lng = float(request.form['lng'])
        except (KeyError, ValueError):
            lat, lng = None, None

        if not lat or not lng:
            lat, lng = extract_exif_gps(temp_filepath)

        if not lat or not lng:
            os.remove(temp_filepath)
            return jsonify({'error': 'No location found. Please pin a location on the map or use a photo with GPS data.'}), 400

        # Upload to Cloudinary, then remove the local temp copy
        try:
            photo_url = save_to_cloudinary(temp_filepath)
        except Exception as e:
            os.remove(temp_filepath)
            return jsonify({'error': f'Upload to Cloudinary failed: {e}'}), 500
        finally:
            if os.path.exists(temp_filepath):
                os.remove(temp_filepath)

        photo_id = uuid.uuid4().hex
        with get_db() as conn:
            conn.execute(
                'INSERT INTO photos (id,filename,photo_url,title,description,category,lat,lng,address,source,uploaded_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)',
                (photo_id, None, photo_url,
                 request.form.get('title','').strip() or None,
                 request.form.get('description','').strip() or None,
                 request.form.get('category','see'),
                 lat, lng,
                 request.form.get('address','').strip() or None,
                 request.form.get('source','').strip() or None,
                 datetime.utcnow().isoformat())
            )
        return jsonify({'id': photo_id, 'redirect': url_for('index'), 'lat': lat, 'lng': lng, 'photo_url': photo_url})

    return render_template('upload.html', kauai=KAUAI)


@app.route('/api/exif_gps', methods=['POST'])
def api_exif_gps():
    file = request.files.get('photo')
    if not file:
        return jsonify({'lat': None, 'lng': None})
    ext = file.filename.rsplit('.', 1)[1].lower() if '.' in file.filename else 'jpg'
    os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)
    tmp_path = os.path.join(app.config['UPLOAD_FOLDER'], f"_tmp_{uuid.uuid4().hex}.{ext}")
    file.save(tmp_path)
    lat, lng = extract_exif_gps(tmp_path)
    os.remove(tmp_path)
    return jsonify({'lat': lat, 'lng': lng})


@app.route('/uploads/<filename>')
def uploaded_file(filename):
    # Kept for backward compatibility with any photos uploaded before the Cloudinary switch
    return send_from_directory(app.config['UPLOAD_FOLDER'], filename)


@app.route('/api/pois')
def api_pois():
    tab  = request.args.get('tab', 'eat')
    term = request.args.get('term', '').lower()

    cached = get_cached_pois(tab)
    if cached is not None:
        results = cached
    else:
        seed_path = os.path.join(os.path.dirname(__file__), 'static', f'data_{tab}.json')
        if os.path.exists(seed_path):
            with open(seed_path) as f:
                data = json.load(f)
            results = parse_osm(data.get('elements', []))
            set_cached_pois(tab, results)
        else:
            results, error = fetch_from_overpass(tab)
            if error:
                return jsonify({'error': error, 'pois': []})
            set_cached_pois(tab, results)

    if term:
        results = [p for p in results if term in p['name'].lower() or term in p['kind'].lower()]

    return jsonify({'pois': results})


@app.route('/api/cache/clear', methods=['POST'])
def clear_cache():
    with get_db() as conn:
        conn.execute('DELETE FROM poi_cache')
    return jsonify({'cleared': True})


@app.route('/api/photos')
def api_photos():
    with get_db() as conn:
        rows = conn.execute('SELECT * FROM photos ORDER BY uploaded_at DESC').fetchall()
    return jsonify([dict(r) for r in rows])


@app.route('/api/photos/<photo_id>', methods=['DELETE'])
def delete_photo(photo_id):
    with get_db() as conn:
        row = conn.execute('SELECT filename FROM photos WHERE id = ?', (photo_id,)).fetchone()
        if not row:
            return jsonify({'error': 'Not found'}), 404
        if row['filename']:
            filepath = os.path.join(app.config['UPLOAD_FOLDER'], row['filename'])
            if os.path.exists(filepath):
                os.remove(filepath)
        conn.execute('DELETE FROM photos WHERE id = ?', (photo_id,))
    return jsonify({'deleted': True})


os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)
init_db()

if __name__ == '__main__':
    app.run(debug=True, port=5051)
