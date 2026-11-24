import os
from functools import wraps
from pathlib import Path

import psycopg
from psycopg.types.json import Jsonb
from flask import Flask, jsonify, request, send_file, session

BASE_DIR = Path(__file__).resolve().parent
app = Flask(__name__)
app.secret_key = os.environ.get('FLASK_SECRET_KEY', 'development-only-change-this-secret')
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE='Lax',
    SESSION_COOKIE_SECURE=os.environ.get('COOKIE_SECURE', 'true').lower() == 'true',
    MAX_CONTENT_LENGTH=25 * 1024 * 1024,
)

DATABASE_URL = os.environ.get('DATABASE_URL', '').strip()
ADMIN_USER = os.environ.get('ADMIN_USER', 'admin')
ADMIN_PASSWORD = os.environ.get('ADMIN_PASSWORD', '')
_DB_INITIALIZED = False


def db_connect():
    global _DB_INITIALIZED
    if not DATABASE_URL:
        raise RuntimeError('Falta configurar DATABASE_URL en el entorno del servidor.')
    url = DATABASE_URL.replace('postgres://', 'postgresql://', 1) if DATABASE_URL.startswith('postgres://') else DATABASE_URL
    conn = psycopg.connect(url, connect_timeout=10)
    if not _DB_INITIALIZED:
        try:
            conn.execute('''
                CREATE TABLE IF NOT EXISTS contaberp_state (
                    id SMALLINT PRIMARY KEY CHECK (id = 1),
                    state JSONB NOT NULL,
                    revision BIGINT NOT NULL DEFAULT 1,
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
            ''')
            conn.execute('ALTER TABLE contaberp_state ADD COLUMN IF NOT EXISTS revision BIGINT NOT NULL DEFAULT 1')
            conn.commit()
            _DB_INITIALIZED = True
        except Exception:
            conn.close()
            raise
    return conn


def init_db():
    with db_connect() as conn:
        conn.execute('SELECT 1')


def login_required(fn):
    @wraps(fn)
    def wrapped(*args, **kwargs):
        if not session.get('contaberp_user'):
            return jsonify(ok=False, error='Sesión vencida. Inicie sesión nuevamente.'), 401
        return fn(*args, **kwargs)
    return wrapped


@app.get('/')
def index():
    return send_file(BASE_DIR / 'ContabERP.html')


@app.get('/api/health')
def health():
    try:
        with db_connect() as conn:
            conn.execute('SELECT 1')
        return jsonify(ok=True, database=True, authenticated=bool(session.get('contaberp_user')), user=session.get('contaberp_user'))
    except Exception as exc:
        app.logger.exception('Database health check failed')
        return jsonify(ok=False, database=False, error=str(exc)), 503


@app.post('/api/login')
def login():
    body = request.get_json(silent=True) or {}
    if not ADMIN_PASSWORD:
        return jsonify(ok=False, error='El administrador debe configurar ADMIN_PASSWORD en el servidor.'), 503
    username = str(body.get('username') or '').strip()
    password = str(body.get('password') or '')
    if username != ADMIN_USER or password != ADMIN_PASSWORD:
        return jsonify(ok=False, error='Usuario o contraseña incorrectos.'), 401
    session.clear()
    session['contaberp_user'] = username
    return jsonify(ok=True, user=username)


@app.post('/api/logout')
def logout():
    session.clear()
    return jsonify(ok=True)


@app.get('/api/state')
@login_required
def get_state():
    try:
        with db_connect() as conn:
            row = conn.execute('SELECT state, revision FROM contaberp_state WHERE id=1').fetchone()
        return jsonify(ok=True, state=row[0] if row else None, revision=int(row[1]) if row else 0)
    except Exception as exc:
        app.logger.exception('Could not load ERP state')
        return jsonify(ok=False, error=f'No se pudo leer PostgreSQL: {exc}'), 503


@app.post('/api/state')
@login_required
def save_state():
    body = request.get_json(silent=True) or {}
    state = body.get('state')
    if not isinstance(state, dict):
        return jsonify(ok=False, error='El estado enviado no tiene un formato válido.'), 400
    state.pop('loggedInUser', None)
    try:
        expected = int(body.get('expectedRevision') or 0)
        with db_connect() as conn:
            row = conn.execute('''
                INSERT INTO contaberp_state (id, state, revision, updated_at)
                VALUES (1, %s, 1, NOW())
                ON CONFLICT (id) DO UPDATE
                  SET state=EXCLUDED.state, revision=contaberp_state.revision + 1, updated_at=NOW()
                  WHERE contaberp_state.revision = %s
                RETURNING revision
            ''', (Jsonb(state), expected)).fetchone()
            if not row:
                current = conn.execute('SELECT revision FROM contaberp_state WHERE id=1').fetchone()
                return jsonify(ok=False, error='Otro usuario guardó cambios primero. La pantalla se actualizará con la información central; revise su último cambio antes de volver a guardarlo.', revision=int(current[0]) if current else 0), 409
        return jsonify(ok=True, saved=True, revision=int(row[0]))
    except Exception as exc:
        app.logger.exception('Could not save ERP state')
        return jsonify(ok=False, error=f'No se pudo guardar en PostgreSQL: {exc}'), 503


with app.app_context():
    try:
        init_db()
        app.logger.info('PostgreSQL listo.')
    except Exception:
        app.logger.exception('PostgreSQL todavía no está disponible; se reintentará al recibir solicitudes.')
