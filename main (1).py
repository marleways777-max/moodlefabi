import os
import json
import subprocess
import threading
import time
import webbrowser
import re
import hashlib
from datetime import datetime
from urllib.parse import quote, urlparse
import socket
import base64
import struct

from plyer import notification

from kivy.app import App
from kivy.uix.boxlayout import BoxLayout
from kivy.uix.gridlayout import GridLayout
from kivy.uix.label import Label
from kivy.uix.textinput import TextInput
from kivy.uix.button import Button
from kivy.uix.checkbox import CheckBox
from kivy.uix.scrollview import ScrollView
from kivy.uix.screenmanager import ScreenManager, Screen
from kivy.graphics import Color, RoundedRectangle, Line, Ellipse, Rectangle, Mesh
from kivy.graphics.texture import Texture
from kivy.uix.widget import Widget
from kivy.clock import Clock
import requests

from bs4 import BeautifulSoup

# ----------------------------------------------------------------------
# CONFIGURACION
# ----------------------------------------------------------------------
# Cada cuantos minutos revisa si hay tareas/tests nuevos o cambios de fecha.
# 180 minutos = cada 3 horas.
INTERVALO_MINUTOS = 180

# Texto grande que aparece en la pantalla de inicio (cámbialo si quieres).
TITULO_PANTALLA = "MOODLE FABI"

# OPCIONAL: para recibir la notificacion tambien en el CELULAR mientras este
# programa corre en tu computadora:
#   1) Instala la app "ntfy" en el celular (Play Store).
#   2) Suscribete a un tema con un nombre unico y dificil de adivinar,
#      por ejemplo: "moodle-fabi-8392".
#   3) Escribe ese mismo nombre aqui abajo.
NTFY_TOPIC = ""

# En el celular los archivos se guardan en la carpeta privada de la app.
try:
    from android.storage import app_storage_path
    CARPETA_APP = app_storage_path()
    EN_ANDROID = True
except Exception:
    CARPETA_APP = os.path.dirname(os.path.abspath(__file__))
    EN_ANDROID = False

# Archivo donde se guarda lo que ya se vio (para no repetir avisos).
ARCHIVO_ESTADO = os.path.join(CARPETA_APP, "moodle_estado.json")

# Chrome propio de la app (perfil separado) para abrir las actividades
# ya con la sesión iniciada. Se crea solo la primera vez.
CDP_PUERTO = 9223
CARPETA_PERFIL_CHROME = os.path.join(CARPETA_APP, "chrome_perfil")

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept-Language": "es-ES,es;q=0.9",
}

MATERIAS_DETECTADAS = []
SESSION_GLOBAL = requests.Session()
LOCK_DATOS = threading.Lock()

DATOS_AUTOLOGIN = {
    "url_base": "",
    "url_login": "",
    "usuario": "",
    "password": "",
}

ETIQUETAS = {"tareas": "Tarea", "tests": "Test", "simuladores": "Simulador"}


# ----------------------------------------------------------------------
# NOTIFICACIONES
# ----------------------------------------------------------------------
def enviar_notificacion(titulo, mensaje, link=None):
    """Notificacion local (plyer) y, si hay NTFY_TOPIC, tambien al celular."""
    try:
        notification.notify(
            title=titulo, message=mensaje[:250], app_name="MOODLE FABI", timeout=10
        )
    except Exception:
        pass

    if NTFY_TOPIC:
        try:
            datos = {
                "topic": NTFY_TOPIC,
                "title": titulo,
                "message": mensaje,
                "priority": 4,
                "tags": ["books"],
            }
            if link:
                datos["click"] = link
            requests.post("https://ntfy.sh", json=datos, timeout=10)
        except Exception:
            pass


def traducir_fechas_a_espanol(texto):
    if not texto:
        return ""
    traducciones = {
        'Sunday': 'Domingo', 'Monday': 'Lunes', 'Tuesday': 'Martes',
        'Wednesday': 'Miércoles', 'Thursday': 'Jueves', 'Friday': 'Viernes', 'Saturday': 'Sábado',
        'January': 'Enero', 'February': 'Febrero', 'March': 'Marzo', 'April': 'Abril',
        'May': 'Mayo', 'June': 'Junio', 'July': 'Julio', 'August': 'Agosto',
        'September': 'Septiembre', 'October': 'Octubre', 'November': 'Noviembre', 'December': 'Diciembre',
        'Due:': 'Fecha límite:'
    }
    for en, es in traducciones.items():
        texto = re.sub(rf'\b{en}\b', es, texto, flags=re.IGNORECASE)
    return texto


# ----------------------------------------------------------------------
# LOGIN Y ESCANEO (sin interfaz)
# ----------------------------------------------------------------------
ARCHIVO_ENTREGADOS = os.path.join(CARPETA_APP, "moodle_entregados.json")

MESES = {
    "enero": 1, "febrero": 2, "marzo": 3, "abril": 4, "mayo": 5, "junio": 6,
    "julio": 7, "agosto": 8, "septiembre": 9, "setiembre": 9, "octubre": 10,
    "noviembre": 11, "diciembre": 12,
}


def cargar_entregados():
    try:
        with open(ARCHIVO_ENTREGADOS, "r", encoding="utf-8") as f:
            return set(json.load(f))
    except Exception:
        return set()


def guardar_entregados():
    try:
        with open(ARCHIVO_ENTREGADOS, "w", encoding="utf-8") as f:
            json.dump(sorted(ENTREGADOS), f, ensure_ascii=False, indent=2)
    except Exception:
        pass


# Enlaces de actividades que ya se vieron como entregadas/hechas.
# Una vez entregada, no se vuelve a consultar.
ENTREGADOS = cargar_entregados()


def parsear_fecha_limite(fechas):
    """Lee la 'Fecha límite: ...' del texto y devuelve un datetime (o None)."""
    if not fechas:
        return None
    m = re.search(r'Fecha límite:\s*(.+)', fechas)
    if not m:
        return None
    d = re.search(
        r'(\d{1,2})\s+(?:de\s+)?([A-Za-záéíóú]+)\s+(?:de\s+)?(\d{4})'
        r'(?:,?\s*(\d{1,2}):(\d{2})\s*(AM|PM)?)?',
        m.group(1), re.IGNORECASE,
    )
    if not d:
        return None
    mes = MESES.get(d.group(2).lower())
    if not mes:
        return None
    hora = int(d.group(4)) if d.group(4) else 23
    minuto = int(d.group(5)) if d.group(5) else 59
    ampm = (d.group(6) or "").upper()
    if ampm == "PM" and hora < 12:
        hora += 12
    elif ampm == "AM" and hora == 12:
        hora = 0
    try:
        return datetime(int(d.group(3)), mes, int(d.group(1)), hora, minuto)
    except ValueError:
        return None


# --- Fechas (entiende el texto en inglés Y en español) ----------------
_PAT_FECHA = (
    r'((?:[A-Za-záéíóúÁÉÍÓÚ]+,\s*)?\d{1,2}\s+(?:de\s+)?[A-Za-záéíóúÁÉÍÓÚ]+\s+(?:de\s+)?\d{4}'
    r'(?:,?\s*\d{1,2}:\d{2}(?:\s*[AP]M)?)?)'
)


def extraer_fechas(texto):
    """
    Saca la fecha de apertura y de cierre de una actividad.
    Moodle puede mostrarlas como: Opened/Opens/Closes/Due (inglés)
    o Abierto/Abre/Cierra/Fecha límite (español).
    """
    ini = re.search(
        r'(?:Opened|Opens|Abierto|Abre|Apertura|Inauguración)\s*:\s*' + _PAT_FECHA,
        texto, re.IGNORECASE)
    fin = re.search(
        r'(?:Closes|Closed|Due|Cierra|Cierre|Cerrado|Fecha límite)\s*:\s*' + _PAT_FECHA,
        texto, re.IGNORECASE)

    partes = []
    if ini:
        partes.append(f"Inauguración: {ini.group(1).strip()}")
    if fin:
        partes.append(f"Fecha límite: {fin.group(1).strip()}")

    if not partes:
        matches = re.findall(
            r'([A-Za-záéíóúÁÉÍÓÚ]+,\s*\d+\s+de\s+[A-Za-z]+\s+de\s+\d{4}[^,\n]*)', texto)
        if len(matches) >= 2:
            partes = [f"Inauguración: {matches[0]}", f"Fecha límite: {matches[1]}"]
        elif len(matches) == 1:
            partes = [f"Fecha límite: {matches[0]}"]

    return traducir_fechas_a_espanol("\n".join(partes))


def clasificar_actividad(nombre, link, texto):
    """
    Decide si es 'tareas', 'tests' o 'simuladores'.
    Importante: los simuladores en Moodle son cuestionarios (/mod/quiz/),
    por eso primero se mira si el NOMBRE dice 'simulador'.
    """
    n = nombre.lower()
    l = link.lower()
    if "simulador" in n:
        return "simuladores"
    if "/mod/assign/" in l:
        return "tareas"
    if "/mod/quiz/" in l:
        return "tests"
    t = n + " " + texto.lower()
    if "simulador" in t:
        return "simuladores"
    if "tarea" in t:
        return "tareas"
    if any(x in t for x in ("test", "prueba", "examen")):
        return "tests"
    return None


def agregar_item(lista, item):
    """Agrega sin repetir. Si ya existía sin fechas y este trae fechas, las completa."""
    for existente in lista:
        if existente["nombre"] == item["nombre"]:
            if not existente.get("fechas") and item.get("fechas"):
                existente["fechas"] = item["fechas"]
            return
    lista.append(item)


def _estado_assign(soup):
    """True = entregada, False = no entregada, None = no se pudo saber."""
    for celda in soup.find_all(['th', 'td']):
        etiqueta = celda.get_text(strip=True).lower()
        if etiqueta in ("submission status", "estado de la entrega",
                        "estado del envío", "estado de envío", "estado de la entrega"):
            sib = celda.find_next_sibling(['td', 'th'])
            if sib is None:
                continue
            est = sib.get_text(" ", strip=True).lower()
            if "borrador" in est or "draft" in est:
                return False
            if ("para calificar" in est or "para calificación" in est
                    or "submitted for grading" in est):
                return True
            if any(x in est for x in ("no se ha", "no attempt", "no submission",
                                      "sin intento", "no hay", "nothing has been")):
                return False
            return None
    texto = soup.get_text(" ", strip=True).lower()
    if any(x in texto for x in ("add submission", "agregar entrega",
                                "añadir entrega", "agregar envío")):
        return False
    return None


def _estado_quiz(soup):
    """
    True = test/simulador hecho, False = no hecho, None = no se pudo saber.
    Funciona con el diseño nuevo de Moodle (tarjetas 'Estado: Finalizado')
    y con el viejo (tabla), en inglés o español.
    """
    # Si la sesión se perdió, Moodle muestra el login: no se puede saber.
    if "logout.php" not in str(soup):
        return None

    # Diseño viejo: tabla de intentos.
    tabla = soup.find('table', class_='quizattemptsummary')
    if tabla:
        for fila in tabla.find_all('tr'):
            t = fila.get_text(" ", strip=True).lower()
            if re.search(r'\b(finished|finalizado|terminado)\b', t):
                return True
        return False

    texto = soup.get_text(" ", strip=True).lower()

    # Algún intento finalizado (Estado: Finalizado / State: Finished).
    if re.search(r'\b(finished|finalizado|terminado)\b', texto):
        return True

    # Hay intentos pero ninguno finalizado (en curso) -> aún no está hecho.
    # Sin intentos tampoco está hecho.
    return False


ARCHIVO_INFO_QUIZ = os.path.join(CARPETA_APP, "moodle_info_quiz.json")


def cargar_info_quiz():
    try:
        with open(ARCHIVO_INFO_QUIZ, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def guardar_info_quiz():
    try:
        with open(ARCHIVO_INFO_QUIZ, "w", encoding="utf-8") as f:
            json.dump(INFO_QUIZ, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


# Intentos permitidos y límite de tiempo de cada test/simulador (por enlace).
INFO_QUIZ = cargar_info_quiz()


def _tiempo_a_espanol(txt):
    partes = []
    for n, u in re.findall(r'(\d+)\s*([A-Za-zñáéíóúÑÁÉÍÓÚ]+)', txt):
        u = u.lower()
        n = int(n)
        if u.startswith(('hour', 'hora')):
            nom = "hora" if n == 1 else "horas"
        elif u.startswith('min'):
            nom = "minuto" if n == 1 else "minutos"
        elif u.startswith(('sec', 'seg')):
            nom = "segundo" if n == 1 else "segundos"
        elif u.startswith(('day', 'día', 'dia')):
            nom = "día" if n == 1 else "días"
        else:
            nom = u
        partes.append(f"{n} {nom}")
    return " ".join(partes) if partes else txt.strip()


def extraer_info_quiz(soup):
    """Lee 'Intentos permitidos' y 'Límite de tiempo' de la página del test."""
    caja = soup.find(class_='quizinfo')
    if caja is None:
        return {}
    texto = caja.get_text("\n", strip=True)
    info = {}
    m = re.search(r'(?:Attempts allowed|Intentos permitidos)\s*:?\s*([^\n]+)', texto, re.IGNORECASE)
    if m:
        val = m.group(1).strip()
        info["intentos"] = "ilimitados" if val.lower() in ("unlimited", "ilimitado", "ilimitados") else val
    else:
        info["intentos"] = "ilimitados"
    m = re.search(r'(?:Time limit|Límite de tiempo)\s*:?\s*([^\n]+)', texto, re.IGNORECASE)
    info["tiempo"] = _tiempo_a_espanol(m.group(1)) if m else "sin límite"
    return info


def consultar_estado(link):
    """
    Abre la actividad y revisa si ya fue entregada/hecha.
    En tests y simuladores también guarda intentos permitidos y límite de tiempo.
    Devuelve True, False o None (no se pudo saber).
    """
    es_quiz = "/mod/quiz/" in link
    if link in ENTREGADOS and (not es_quiz or link in INFO_QUIZ):
        return True
    if "/mod/assign/" in link:
        funcion = _estado_assign
    elif es_quiz:
        funcion = _estado_quiz
    else:
        return None
    try:
        resp = SESSION_GLOBAL.get(link, timeout=10)
        if resp.status_code != 200:
            return None
        soup = BeautifulSoup(resp.text, 'html.parser')
        resultado = funcion(soup)
        if es_quiz and resultado is not None:
            info = extraer_info_quiz(soup)
            if info:
                INFO_QUIZ[link] = info
                guardar_info_quiz()
    except Exception:
        return None
    if resultado is True:
        ENTREGADOS.add(link)
        guardar_entregados()
    return resultado


def descripcion_estado(item, cat):
    """Devuelve (texto, color_hex) con el estado que se muestra en pantalla."""
    if cat == "tareas":
        ok = "Ya está entregada esta tarea"
        tarde = "NO ENTREGADA: ya pasó la fecha"
        falta = "Aún no está entregada esta tarea"
    elif cat == "tests":
        ok = "Ya está hecho este test"
        tarde = "NO REALIZADO: ya pasó la fecha"
        falta = "Aún no está hecho este test"
    else:
        ok = "Ya está hecho este simulador"
        tarde = "NO REALIZADO: ya pasó la fecha"
        falta = "Aún no está hecho este simulador"

    entregado = item.get("entregado")
    if entregado is True:
        return ok, "00e676"
    if entregado is False:
        limite = parsear_fecha_limite(item.get("fechas", ""))
        if limite and limite < datetime.now():
            return tarde, "ff4060"
        return falta, "ff6b6b"
    return "Estado no disponible", "8899aa"


# ----------------------------------------------------------------------
# ABRIR LA ACTIVIDAD DIRECTO (con la sesión ya iniciada)
# ----------------------------------------------------------------------
def asegurar_sesion():
    """Si la sesión de Moodle se venció, vuelve a iniciarla."""
    try:
        base = DATOS_AUTOLOGIN["url_base"]
        resp = SESSION_GLOBAL.get(f"{base}/my/", timeout=10)
        if "logout.php" not in resp.text and DATOS_AUTOLOGIN["usuario"]:
            iniciar_sesion_web(
                DATOS_AUTOLOGIN["url_login"],
                DATOS_AUTOLOGIN["usuario"],
                DATOS_AUTOLOGIN["password"],
            )
    except Exception:
        pass


def encontrar_chrome():
    rutas = [
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        os.path.expanduser(r"~\AppData\Local\Google\Chrome\Application\chrome.exe"),
    ]
    for r in rutas:
        if os.path.exists(r):
            return r
    return None


def _chrome_listo():
    try:
        r = requests.get(f"http://127.0.0.1:{CDP_PUERTO}/json/version", timeout=1)
        return r.status_code == 200
    except Exception:
        return False


def registrar_log(texto):
    try:
        with open(os.path.join(CARPETA_APP, "moodle_fabi_log.txt"), "a", encoding="utf-8") as f:
            f.write(f"{datetime.now():%Y-%m-%d %H:%M:%S}  {texto}\n")
    except Exception:
        pass


class MiniWS:
    """Cliente WebSocket mínimo (no necesita instalar nada extra)."""

    def __init__(self, ws_url, timeout=10):
        u = urlparse(ws_url)
        self.sock = socket.create_connection((u.hostname, u.port), timeout=timeout)
        key = base64.b64encode(os.urandom(16)).decode()
        path = u.path + (("?" + u.query) if u.query else "")
        req = (
            f"GET {path} HTTP/1.1\r\nHost: {u.hostname}:{u.port}\r\n"
            f"Upgrade: websocket\r\nConnection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n"
        )
        self.sock.sendall(req.encode())
        resp = b""
        while b"\r\n\r\n" not in resp:
            chunk = self.sock.recv(1024)
            if not chunk:
                raise ConnectionError("Chrome cerró la conexión de control")
            resp += chunk
        cabecera, _, resto = resp.partition(b"\r\n\r\n")
        if b" 101 " not in cabecera.split(b"\r\n")[0]:
            raise ConnectionError("Chrome rechazó la conexión de control")
        self.buf = resto

    def _leer(self, n):
        while len(self.buf) < n:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise ConnectionError("conexión cerrada")
            self.buf += chunk
        datos, self.buf = self.buf[:n], self.buf[n:]
        return datos

    def send(self, texto):
        datos = texto.encode("utf-8")
        n = len(datos)
        cab = bytearray([0x81])
        if n < 126:
            cab.append(0x80 | n)
        elif n < 65536:
            cab.append(0x80 | 126)
            cab += struct.pack(">H", n)
        else:
            cab.append(0x80 | 127)
            cab += struct.pack(">Q", n)
        mascara = os.urandom(4)
        cab += mascara
        cuerpo = bytes(b ^ mascara[i % 4] for i, b in enumerate(datos))
        self.sock.sendall(bytes(cab) + cuerpo)

    def recv(self):
        msg = b""
        while True:
            b1, b2 = self._leer(2)
            fin = b1 & 0x80
            op = b1 & 0x0F
            n = b2 & 0x7F
            if n == 126:
                n = struct.unpack(">H", self._leer(2))[0]
            elif n == 127:
                n = struct.unpack(">Q", self._leer(8))[0]
            datos = self._leer(n)
            if op == 0x8:
                raise ConnectionError("conexión cerrada")
            if op in (0x0, 0x1, 0x2):
                msg += datos
                if fin:
                    return msg.decode("utf-8", "ignore")

    def close(self):
        try:
            self.sock.close()
        except Exception:
            pass


def abrir_con_sesion(url):
    """
    Abre `url` en un Chrome de la app, YA LOGUEADO: copia las cookies de la
    sesión que la app tiene iniciada y luego va directo a la actividad.
    Devuelve (True, "") o (False, motivo_del_fallo).
    """
    chrome = encontrar_chrome()
    if not chrome:
        return False, "No encontré Chrome instalado"

    ws = None
    try:
        if not _chrome_listo():
            subprocess.Popen([
                chrome,
                f"--remote-debugging-port={CDP_PUERTO}",
                f"--user-data-dir={CARPETA_PERFIL_CHROME}",
                "--remote-allow-origins=*",
                "--no-first-run",
                "--no-default-browser-check",
                "about:blank",
            ])
            for _ in range(80):
                if _chrome_listo():
                    break
                time.sleep(0.25)
            else:
                return False, "Chrome no abrió su puerto de control"

        info = requests.get(f"http://127.0.0.1:{CDP_PUERTO}/json/version", timeout=5).json()
        ws = MiniWS(info["webSocketDebuggerUrl"])
        contador = [0]

        def llamar(metodo, params=None):
            contador[0] += 1
            ident = contador[0]
            ws.send(json.dumps({"id": ident, "method": metodo, "params": params or {}}))
            while True:
                msg = json.loads(ws.recv())
                if msg.get("id") == ident:
                    return msg

        base = DATOS_AUTOLOGIN["url_base"]
        host = urlparse(base).hostname or ""
        cookies = []
        for c in SESSION_GLOBAL.cookies:
            dominio = (c.domain or "").lstrip(".")
            if dominio and dominio not in host and host not in dominio:
                continue
            cookies.append({
                "name": c.name,
                "value": c.value,
                "url": base + "/",
                "path": c.path or "/",
                "secure": base.startswith("https"),
                "httpOnly": True,
                "expires": time.time() + 86400,
            })
        if not cookies:
            return False, "La app no tiene cookies de sesión"

        r = llamar("Storage.setCookies", {"cookies": cookies})
        if "error" in r:
            return False, "No pude pasar la sesión: " + str(r["error"].get("message"))

        r = llamar("Target.createTarget", {"url": url})
        if "error" in r:
            return False, "No pude abrir la pestaña: " + str(r["error"].get("message"))
        try:
            llamar("Target.activateTarget", {"targetId": r["result"]["targetId"]})
        except Exception:
            pass
        return True, ""
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"[:120]
    finally:
        if ws:
            ws.close()


def obtener_url_autologin(destino):
    """
    (Respaldo) Pide a Moodle un enlace de acceso automático usando el servicio
    de la app móvil oficial. Devuelve None si la plataforma no lo permite.
    """
    try:
        base = DATOS_AUTOLOGIN["url_base"]
        t = requests.post(
            f"{base}/login/token.php",
            data={
                "username": DATOS_AUTOLOGIN["usuario"],
                "password": DATOS_AUTOLOGIN["password"],
                "service": "moodle_mobile_app",
            },
            timeout=10,
        ).json()
        token = t.get("token")
        privado = t.get("privatetoken")
        if not token or not privado:
            return None

        ws = f"{base}/webservice/rest/server.php"
        info = requests.post(
            ws,
            data={"wstoken": token, "wsfunction": "core_webservice_get_site_info",
                  "moodlewsrestformat": "json"},
            timeout=10,
        ).json()
        userid = info.get("userid")

        clave = requests.post(
            ws,
            data={"wstoken": token, "wsfunction": "tool_mobile_get_autologin_key",
                  "privatetoken": privado, "moodlewsrestformat": "json"},
            timeout=10,
        ).json()
        key = clave.get("key")
        url_auto = clave.get("autologinurl")
        if not (userid and key and url_auto):
            return None
        return f"{url_auto}?userid={userid}&key={key}&urltogo={quote(destino, safe='')}"
    except Exception:
        return None


def fuente_bonita():
    """Busca una letra elegante instalada en Windows; si no hay, usa la normal."""
    carpeta = os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Fonts")
    for nombre in ("GOTHIC.TTF", "georgia.ttf", "pala.ttf", "segoeui.ttf"):
        ruta_fuente = os.path.join(carpeta, nombre)
        if os.path.exists(ruta_fuente):
            return ruta_fuente
    return None


def espaciar(texto):
    """'HOLA MUNDO' -> 'H O L A    M U N D O' (letras separadas, se ve más elegante)."""
    return "    ".join(" ".join(palabra) for palabra in texto.split())


def iniciar_sesion_web(url_login, usuario, password):
    """Inicia sesion en Moodle y devuelve True si entro de verdad."""
    SESSION_GLOBAL.headers.update(HEADERS)
    resp = SESSION_GLOBAL.get(url_login, timeout=15)
    soup = BeautifulSoup(resp.text, 'html.parser')
    token_input = soup.find('input', {'name': 'logintoken'})
    logintoken = token_input['value'] if token_input else ''
    payload = {'username': usuario, 'password': password, 'logintoken': logintoken}
    post_resp = SESSION_GLOBAL.post(url_login, data=payload, timeout=15)
    texto = post_resp.text
    return "logout.php" in texto or "Cerrar sesión" in texto


def escanear_materias(al_avanzar=None):
    """
    Recorre las materias y llena MATERIAS_DETECTADAS con tareas, tests y simuladores.
    Devuelve True si encontro materias.
    al_avanzar(i, total, nombre) se llama al terminar cada materia para ir
    mostrando los resultados en pantalla sin esperar a que termine todo.
    """
    global MATERIAS_DETECTADAS
    base_domain = DATOS_AUTOLOGIN["url_base"]
    dashboard_url = f"{base_domain}/my/"

    def obtener_cursos():
        resp_dash = SESSION_GLOBAL.get(dashboard_url, timeout=15)
        soup_dash = BeautifulSoup(resp_dash.text, 'html.parser')
        enlaces = soup_dash.find_all('a', href=re.compile(r'course/view\.php\?id=\d+'))
        cursos = {}
        for tag in enlaces:
            match = re.search(r'id=(\d+)', tag['href'])
            if match:
                course_id = match.group(1)
                nombre = tag.get_text(strip=True)
                if course_id not in cursos and len(nombre) > 3:
                    cursos[course_id] = {
                        "id": course_id,
                        "nombre": nombre,
                        "tareas": [],
                        "tests": [],
                        "simuladores": [],
                    }
        return cursos

    materias_dict = obtener_cursos()

    # Si la sesion se vencio, vuelve a iniciar sesion una vez.
    if not materias_dict and DATOS_AUTOLOGIN["usuario"]:
        try:
            iniciar_sesion_web(
                DATOS_AUTOLOGIN["url_login"],
                DATOS_AUTOLOGIN["usuario"],
                DATOS_AUTOLOGIN["password"],
            )
            materias_dict = obtener_cursos()
        except Exception:
            pass

    if not materias_dict:
        return False

    materias = list(materias_dict.values())

    # Reutiliza lo ya conocido para que la pantalla no se vacíe mientras escanea.
    with LOCK_DATOS:
        previas = {m["id"]: m for m in MATERIAS_DETECTADAS}
    for mat in materias:
        anterior = previas.get(mat["id"])
        if anterior:
            mat["tareas"] = list(anterior.get("tareas", []))
            mat["tests"] = list(anterior.get("tests", []))
            mat["simuladores"] = list(anterior.get("simuladores", []))

    with LOCK_DATOS:
        MATERIAS_DETECTADAS = materias
    if al_avanzar:
        al_avanzar(0, len(materias), "")

    for idx, mat in enumerate(materias, 1):
        materia_id = mat["id"]
        encontrados = {"tareas": [], "tests": [], "simuladores": []}

        for sec_num in range(0, 30):
            url_seccion = f"{base_domain}/course/view.php?id={materia_id}&section={sec_num}"
            try:
                resp = SESSION_GLOBAL.get(url_seccion, timeout=6)
                if resp.status_code == 404:
                    break  # ya no hay más secciones en esta materia
                if resp.status_code != 200:
                    continue
                soup = BeautifulSoup(resp.text, 'html.parser')
                actividades = soup.find_all(['li', 'div', 'a'], class_=['activity', 'activity-item', 'card', 'aalink'])

                for act in actividades:
                    texto_act = act.get_text(separator=' ', strip=True)
                    if not texto_act or len(texto_act) < 3:
                        continue

                    # Prefiere el enlace que apunta a la actividad (/mod/...).
                    enlace_tag = None
                    if act.name == 'a' and '/mod/' in (act.get('href') or ''):
                        enlace_tag = act
                    else:
                        enlace_tag = act.find('a', href=re.compile(r'/mod/'))
                    if enlace_tag is None:
                        enlace_tag = act if act.name == 'a' else act.find('a')
                    link = enlace_tag.get('href', url_seccion) if enlace_tag else url_seccion

                    nombre_tag = act.find(['span', 'h3', 'h4', 'a'], class_=['instancename', 'activityname'])
                    raw_nombre = nombre_tag.get_text(strip=True) if nombre_tag else ""
                    if not raw_nombre and enlace_tag:
                        raw_nombre = enlace_tag.get_text(strip=True)
                    if not raw_nombre:
                        raw_nombre = texto_act.split('\n')[0]

                    nombre_limpio = re.sub(r'(Assignment|Quiz|File|Page|URL)$', '', raw_nombre, flags=re.IGNORECASE).strip()

                    item_data = {
                        "nombre": nombre_limpio,
                        "link": link,
                        "fechas": extraer_fechas(texto_act),
                    }

                    categoria = clasificar_actividad(nombre_limpio, link, texto_act)
                    if categoria:
                        agregar_item(encontrados[categoria], item_data)
            except Exception:
                pass

        for cat in ("tareas", "tests", "simuladores"):
            for it in encontrados[cat]:
                it["entregado"] = consultar_estado(it["link"])
                it["info"] = INFO_QUIZ.get(it["link"])
            mat[cat] = encontrados[cat]

        if al_avanzar:
            al_avanzar(idx, len(materias), mat["nombre"])

    with LOCK_DATOS:
        MATERIAS_DETECTADAS = materias
    return True


# ----------------------------------------------------------------------
# DETECCION DE NOVEDADES
# ----------------------------------------------------------------------
def cargar_estado():
    """Devuelve el dict guardado, o None si es la primera vez."""
    try:
        with open(ARCHIVO_ESTADO, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def guardar_estado(estado):
    try:
        with open(ARCHIVO_ESTADO, "w", encoding="utf-8") as f:
            json.dump(estado, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


def revisar_novedades_y_notificar():
    """
    Compara lo escaneado con lo guardado. Avisa cuando:
      - aparece una tarea/test/simulador nuevo
      - cambian las fechas de uno que ya existia
    La primera vez solo guarda lo que hay (para no llenarte de avisos).
    """
    previo = cargar_estado()
    nuevo_estado = dict(previo) if previo else {}
    novedades = []

    # Cada plataforma + usuario tiene su propio registro.
    huella = hashlib.md5(DATOS_AUTOLOGIN["usuario"].encode("utf-8")).hexdigest()[:8]
    prefijo = f"{DATOS_AUTOLOGIN['url_base']}|{huella}|"
    primera_vez = previo is None or not any(k.startswith(prefijo) for k in previo)

    with LOCK_DATOS:
        materias = list(MATERIAS_DETECTADAS)

    for mat in materias:
        for cat in ("tareas", "tests", "simuladores"):
            for item in mat.get(cat, []):
                clave = f"{prefijo}{mat['id']}|{cat}|{item['nombre']}"
                fechas = item.get("fechas", "")
                if not primera_vez:
                    if clave not in previo:
                        novedades.append(("nuevo", mat, cat, item))
                    elif previo[clave] != fechas:
                        novedades.append(("fechas", mat, cat, item))
                nuevo_estado[clave] = fechas

    guardar_estado(nuevo_estado)

    if primera_vez:
        total = sum(1 for k in nuevo_estado if k.startswith(prefijo))
        enviar_notificacion(
            "MOODLE FABI listo",
            f"Registré {total} actividades. Te avisaré cuando aparezca algo nuevo.",
        )
        return 0

    for tipo, mat, cat, item in novedades:
        etiqueta = ETIQUETAS.get(cat, "Actividad")
        titulo = f"Nueva {etiqueta.lower()}" if tipo == "nuevo" else f"Fechas actualizadas ({etiqueta.lower()})"
        if cat != "tareas":
            titulo = f"Nuevo {etiqueta.lower()}" if tipo == "nuevo" else titulo
        cuerpo = f"{mat['nombre']}\n{item['nombre']}"
        if item.get("fechas"):
            cuerpo += f"\n{item['fechas']}"
        cuerpo += f"\nDetectado: {datetime.now():%d/%m/%Y %H:%M}"
        enviar_notificacion(titulo, cuerpo, item.get("link"))
        time.sleep(1)

    return len(novedades)


# ----------------------------------------------------------------------
# WIDGETS PERSONALIZADOS
# ----------------------------------------------------------------------
class TarjetaDios(BoxLayout):
    def __init__(self, border_color=(0, 0.85, 1, 0.6), bg_color=(0.06, 0.08, 0.15, 0.95), **kwargs):
        super(TarjetaDios, self).__init__(**kwargs)
        self.border_color = border_color
        self.bg_color = bg_color

        with self.canvas.before:
            Color(*self.bg_color)
            self.bg = RoundedRectangle(pos=self.pos, size=self.size, radius=[16])
            Color(*self.border_color)
            self.border = Line(rounded_rectangle=(self.pos[0], self.pos[1], self.size[0], self.size[1], 16), width=1.5)

        self.bind(pos=self._update_graphics, size=self._update_graphics)

    def _update_graphics(self, instance, value):
        self.bg.pos = self.pos
        self.bg.size = self.size
        self.border.rounded_rectangle = (self.pos[0], self.pos[1], self.size[0], self.size[1], 16)


class BotonDios(Button):
    def __init__(self, main_color=(0, 0.6, 1, 1), **kwargs):
        super(BotonDios, self).__init__(**kwargs)
        self.main_color = main_color
        self.background_color = (0, 0, 0, 0)
        self.background_normal = ''

        with self.canvas.before:
            Color(*self.main_color)
            self.rect = RoundedRectangle(pos=self.pos, size=self.size, radius=[12])
            Color(1, 1, 1, 0.25)
            self.line = Line(rounded_rectangle=(self.pos[0], self.pos[1], self.size[0], self.size[1], 12), width=1)

        self.bind(pos=self._update_graphics, size=self._update_graphics)

    def _update_graphics(self, instance, value):
        self.rect.pos = self.pos
        self.rect.size = self.size
        self.line.rounded_rectangle = (self.pos[0], self.pos[1], self.size[0], self.size[1], 12)


# ----------------------------------------------------------------------
# PANTALLA DE LOGIN
# ----------------------------------------------------------------------
class PanelBiselado(BoxLayout):
    """Panel oscuro con esquinas cortadas y borde de neón."""

    def __init__(self, corte=18, **kwargs):
        super(PanelBiselado, self).__init__(**kwargs)
        self.corte = corte
        with self.canvas.before:
            Color(0.07, 0.17, 0.25, 0.95)
            self.relleno = Mesh(vertices=[], indices=[], mode='triangle_fan')
            Color(0.39, 0.74, 0.83, 0.95)
            self.borde = Line(points=[], width=1.8, close=True)
            Color(0.75, 0.3, 0.95, 0.55)
            self.borde2 = Line(points=[], width=1.0, close=True)
        self.bind(pos=self._actualizar, size=self._actualizar)

    def _puntos(self, inset=0):
        x, y = self.pos[0] + inset, self.pos[1] + inset
        w, h = self.size[0] - 2 * inset, self.size[1] - 2 * inset
        c = self.corte
        return [
            (x + c, y), (x + w - c, y), (x + w, y + c), (x + w, y + h - c),
            (x + w - c, y + h), (x + c, y + h), (x, y + h - c), (x, y + c),
        ]

    def _actualizar(self, *args):
        pts = self._puntos(0)
        cx, cy = self.center
        verts = [cx, cy, 0, 0]
        for (px, py) in pts + [pts[0]]:
            verts += [px, py, 0, 0]
        self.relleno.vertices = verts
        self.relleno.indices = list(range(len(verts) // 4))
        self.borde.points = [v for p in pts for v in p]
        self.borde2.points = [v for p in self._puntos(5) for v in p]


class CampoNeon(BoxLayout):
    """Marco de neón para un campo de texto."""

    def __init__(self, **kwargs):
        kwargs.setdefault('padding', [2, 2, 2, 2])
        super(CampoNeon, self).__init__(**kwargs)
        with self.canvas.before:
            Color(0.08, 0.2, 0.3, 1)
            self.fondo = RoundedRectangle(pos=self.pos, size=self.size, radius=[8])
            Color(0.3, 0.75, 0.9, 0.85)
            self.marco = Line(rounded_rectangle=(self.pos[0], self.pos[1], self.size[0], self.size[1], 8), width=1.3)
        self.bind(pos=self._actualizar, size=self._actualizar)

    def _actualizar(self, *args):
        self.fondo.pos = self.pos
        self.fondo.size = self.size
        self.marco.rounded_rectangle = (self.pos[0], self.pos[1], self.size[0], self.size[1], 8)


class BotonNeon(Button):
    """Botón rosado con brillo y borde de neón."""

    def __init__(self, main_color=(0.55, 0.08, 0.31, 1), borde=(1, 0.55, 0.8, 0.9), **kwargs):
        super(BotonNeon, self).__init__(**kwargs)
        self.background_color = (0, 0, 0, 0)
        self.background_normal = ''
        self.background_down = ''
        with self.canvas.before:
            Color(borde[0], borde[1], borde[2], 0.25)
            self.glow = RoundedRectangle(pos=self.pos, size=self.size, radius=[18])
            Color(*main_color)
            self.rect = RoundedRectangle(pos=self.pos, size=self.size, radius=[14])
            Color(1, 1, 1, 0.16)
            self.brillo = RoundedRectangle(pos=self.pos, size=self.size, radius=[14, 14, 0, 0])
            Color(*borde)
            self.linea = Line(rounded_rectangle=(self.pos[0], self.pos[1], self.size[0], self.size[1], 14), width=1.6)
        self.bind(pos=self._actualizar, size=self._actualizar)

    def _actualizar(self, *args):
        x, y = self.pos
        w, h = self.size
        self.glow.pos = (x - 3, y - 3)
        self.glow.size = (w + 6, h + 6)
        self.rect.pos = self.pos
        self.rect.size = self.size
        self.brillo.pos = (x, y + h / 2)
        self.brillo.size = (w, h / 2)
        self.linea.rounded_rectangle = (x, y, w, h, 14)


class LoginScreen(Screen):
    def __init__(self, **kwargs):
        super(LoginScreen, self).__init__(**kwargs)

        # Fondo con degradado azul oscuro y detalles tipo panel futurista.
        tex = Texture.create(size=(1, 2), colorfmt='rgba')
        tex.blit_buffer(bytes([4, 12, 26, 255, 10, 40, 64, 255]), colorfmt='rgba', bufferfmt='ubyte')
        tex.mag_filter = 'linear'

        with self.canvas.before:
            Color(1, 1, 1, 1)
            self.bg_rect = Rectangle(texture=tex, pos=self.pos, size=self.size)
            Color(0.1, 0.8, 1, 0.22)
            self.aro1 = Line(circle=(0, 0, 10), width=1.5)
            Color(0.1, 0.8, 1, 0.14)
            self.aro2 = Line(circle=(0, 0, 10), width=1.2)
            Color(0.8, 0.3, 1, 0.16)
            self.aro3 = Line(circle=(0, 0, 10), width=1.2)
            Color(0.1, 0.8, 1, 0.10)
            self.barras = [Rectangle(pos=(0, 0), size=(10, 10)) for _ in range(12)]
            Color(0.1, 0.9, 1, 0.55)
            self.linea_base1 = Line(points=[0, 0, 0, 0], width=1.5)
            Color(0.9, 0.3, 0.9, 0.55)
            self.linea_base2 = Line(points=[0, 0, 0, 0], width=1.5)

        self.bind(pos=self._update_bg, size=self._update_bg)

        main_layout = BoxLayout(orientation='vertical', padding=[28, 22, 28, 22], spacing=12)

        header_box = BoxLayout(orientation='vertical', size_hint_y=None, height=156, spacing=0)
        header_box.add_widget(Label(
            text=espaciar("ELABORADO POR FABIOLA FLORES"),
            font_name=fuente_bonita() or "Roboto",
            font_size=15,
            color=(0.72, 0.93, 1, 1),
            size_hint_y=None,
            height=28,
        ))
        header_box.add_widget(Label(
            text=TITULO_PANTALLA, font_size=58, bold=True,
            color=(0.08, 0.2, 0.32, 1), outline_width=3, outline_color=(0.95, 0.78, 0.4, 1)
        ))
        main_layout.add_widget(header_box)
        main_layout.add_widget(Widget(size_hint_y=1))

        form_card = PanelBiselado(
            orientation='vertical',
            padding=[24, 20, 24, 20],
            spacing=8,
            size_hint_y=None,
            height=320,
        )

        def etiqueta(texto):
            lbl = Label(text=texto, font_size=12, bold=True, color=(0.6, 0.85, 1, 1),
                        size_hint_y=None, height=18, halign='left')
            lbl.bind(size=lbl.setter('text_size'))
            return lbl

        def campo_texto(**kw):
            return TextInput(
                multiline=False,
                write_tab=False,
                background_normal='',
                background_active='',
                background_color=(0.08, 0.2, 0.3, 1),
                foreground_color=(0.9, 0.97, 1, 1),
                hint_text_color=(0.45, 0.62, 0.72, 1),
                cursor_color=(0.2, 0.9, 1, 1),
                font_size=20,
                padding=[14, 14, 14, 14],
                **kw
            )

        form_card.add_widget(etiqueta("Link de la universidad"))
        row_url = BoxLayout(orientation='horizontal', spacing=8, size_hint_y=None, height=60)
        self.input_url = campo_texto(
            text="",
            hint_text="Ej: https://aula.tuuniversidad.edu/login/index.php",
        )
        self.chk_url = CheckBox(active=False, size_hint_x=None, width=35, color=(0, 0.9, 1, 1))
        self.chk_url.bind(active=self.on_chk_url_change)
        marco_url = CampoNeon()
        marco_url.add_widget(self.input_url)
        row_url.add_widget(marco_url)
        row_url.add_widget(self.chk_url)
        form_card.add_widget(row_url)

        form_card.add_widget(etiqueta("Usuario"))
        row_user = BoxLayout(orientation='horizontal', spacing=8, size_hint_y=None, height=60)
        self.input_usuario = campo_texto(text="", hint_text="Ingresa tu usuario institucional")
        self.chk_user = CheckBox(active=False, size_hint_x=None, width=35, color=(0, 0.9, 1, 1))
        self.chk_user.bind(active=self.on_chk_user_change)
        marco_user = CampoNeon()
        marco_user.add_widget(self.input_usuario)
        row_user.add_widget(marco_user)
        row_user.add_widget(self.chk_user)
        form_card.add_widget(row_user)

        form_card.add_widget(etiqueta("Contraseña"))
        row_pass = BoxLayout(orientation='horizontal', spacing=6, size_hint_y=None, height=60)
        self.input_password = campo_texto(text="", hint_text="Ingresa tu contraseña", password=True)
        marco_pass = CampoNeon()
        marco_pass.add_widget(self.input_password)

        self.btn_eye = BotonDios(
            text="👁",
            main_color=(0.06, 0.2, 0.32, 1),
            color=(0, 0.9, 1, 1),
            size_hint_x=None,
            width=56,
            font_size=14
        )
        self.btn_eye.bind(on_press=self.toggle_password_visibility)

        self.chk_pass = CheckBox(active=False, size_hint_x=None, width=35, color=(0, 0.9, 1, 1))
        self.chk_pass.bind(active=self.on_chk_pass_change)

        row_pass.add_widget(marco_pass)
        row_pass.add_widget(self.btn_eye)
        row_pass.add_widget(self.chk_pass)
        form_card.add_widget(row_pass)

        self.input_url.bind(on_text_validate=lambda instance: setattr(self.input_usuario, 'focus', True))
        self.input_usuario.bind(on_text_validate=lambda instance: setattr(self.input_password, 'focus', True))
        self.input_password.bind(on_text_validate=self.intentar_conexion)

        main_layout.add_widget(form_card)

        self.lbl_estado = Label(text="Ingresa tus credenciales para continuar.", color=(0.6, 0.7, 0.9, 1), size_hint_y=None, height=22, font_size=12)
        main_layout.add_widget(self.lbl_estado)
        main_layout.add_widget(Widget(size_hint_y=1))

        btn_login = BotonNeon(
            text="INICIAR SESION", color=(1, 1, 1, 1), bold=True, font_size=17,
            size_hint_y=None, height=56
        )
        btn_login.bind(on_press=self.intentar_conexion)
        main_layout.add_widget(btn_login)

        self.add_widget(main_layout)

    def _update_bg(self, instance, value):
        x, y = self.pos
        w, h = self.size
        self.bg_rect.pos = self.pos
        self.bg_rect.size = self.size
        cx, cy = x + w * 0.84, y + h * 0.86
        base = min(w, h)
        self.aro1.circle = (cx, cy, base * 0.14)
        self.aro2.circle = (cx, cy, base * 0.22)
        self.aro3.circle = (cx, cy, base * 0.30)
        for i, barra in enumerate(self.barras):
            alto = 18 + (i * 37) % 70
            barra.pos = (x + w * 0.04 + i * w * 0.02, y + h * 0.80)
            barra.size = (w * 0.012, alto)
        self.linea_base1.points = [x + w * 0.05, y + h * 0.03, x + w * 0.35, y + h * 0.03]
        self.linea_base2.points = [x + w * 0.65, y + h * 0.03, x + w * 0.95, y + h * 0.03]

    def toggle_password_visibility(self, instance):
        self.input_password.password = not self.input_password.password

    def on_chk_url_change(self, checkbox, value):
        if not value:
            self.input_url.text = ""

    def on_chk_user_change(self, checkbox, value):
        if not value:
            self.input_usuario.text = ""

    def on_chk_pass_change(self, checkbox, value):
        if not value:
            self.input_password.text = ""

    def intentar_conexion(self, instance):
        url = self.input_url.text.strip()
        usuario = self.input_usuario.text.strip()
        password = self.input_password.text.strip()
        if not url or not usuario or not password:
            self.lbl_estado.text = "Por favor completa todos los campos."
            self.lbl_estado.color = (1, 0.3, 0.4, 1)
            return

        # Acepta "aula.universidad.edu", "https://aula.universidad.edu" o el link completo.
        if not url.lower().startswith("http"):
            url = "https://" + url
        if "/login" not in url:
            url = url.rstrip("/") + "/login/index.php"

        DATOS_AUTOLOGIN["usuario"] = usuario
        DATOS_AUTOLOGIN["password"] = password
        DATOS_AUTOLOGIN["url_login"] = url
        DATOS_AUTOLOGIN["url_base"] = url.split("/login")[0]

        self.lbl_estado.text = "Conectando al servidor..."
        self.lbl_estado.color = (1, 0.8, 0, 1)
        threading.Thread(
            target=self.proceso_login_segundo_plano,
            args=(url, usuario, password),
            daemon=True,
        ).start()

    def proceso_login_segundo_plano(self, url, usuario, password):
        try:
            if iniciar_sesion_web(url, usuario, password):
                enviar_notificacion(
                    "Moodle conectado",
                    "Revisando tareas, tests y simuladores nuevos...",
                )
                Clock.schedule_once(lambda dt: self.cambiar_a_dashboard(), 0.1)
            else:
                Clock.schedule_once(
                    lambda dt: self.mostrar_error("Credenciales incorrectas."), 0.1
                )
        except Exception as e:
            msg = f"No se pudo conectar: {e}"[:90]
            Clock.schedule_once(lambda dt, m=msg: self.mostrar_error(m), 0.1)

    def mostrar_error(self, mensaje):
        self.lbl_estado.text = mensaje
        self.lbl_estado.color = (1, 0.2, 0.5, 1)

    def activar_modo_respaldo_local(self):
        self.lbl_estado.text = "Acceso confirmado."
        self.lbl_estado.color = (0, 1, 0.5, 1)
        Clock.schedule_once(lambda dt: self.cambiar_a_dashboard(), 0.5)

    def cambiar_a_dashboard(self):
        self.manager.current = 'dashboard'


# ----------------------------------------------------------------------
# PANTALLA PRINCIPAL
# ----------------------------------------------------------------------
class DashboardScreen(Screen):
    def __init__(self, **kwargs):
        super(DashboardScreen, self).__init__(**kwargs)
        self.categoria_actual = "TAREAS"
        self.monitor_activo = False
        self.escaneando = False

        with self.canvas.before:
            Color(0.04, 0.05, 0.1, 1)
            self.bg_rect = RoundedRectangle(pos=self.pos, size=self.size, radius=[0])
        self.bind(pos=self._update_bg, size=self._update_bg)

        layout = BoxLayout(orientation='vertical', padding=15, spacing=12)

        top_bar = TarjetaDios(orientation='horizontal', size_hint_y=None, height=50, padding=[12, 6, 12, 6], border_color=(0, 0.85, 1, 0.4), bg_color=(0.07, 0.09, 0.18, 0.9))
        top_bar.add_widget(Label(text="MOODLE FABI", font_size=15, bold=True, color=(0, 0.9, 1, 1)))

        btn_salir = BotonDios(text="SALIR", main_color=(0.85, 0.1, 0.3, 1), size_hint_x=None, width=75, color=(1, 1, 1, 1), bold=True, font_size=11)
        btn_salir.bind(on_press=self.cerrar_sesion)
        top_bar.add_widget(btn_salir)
        layout.add_widget(top_bar)

        filtros_bar = BoxLayout(size_hint_y=None, height=42, spacing=8)
        btn_tareas = BotonDios(text="TAREAS", main_color=(0, 0.5, 0.9, 1), bold=True, font_size=12)
        btn_tareas.bind(on_press=lambda x: self.cambiar_categoria("TAREAS"))

        btn_tests = BotonDios(text="TESTS", main_color=(0.9, 0.3, 0, 1), bold=True, font_size=12)
        btn_tests.bind(on_press=lambda x: self.cambiar_categoria("TESTS"))

        btn_simuladores = BotonDios(text="SIMULADORES", main_color=(0.6, 0.1, 0.8, 1), bold=True, font_size=12)
        btn_simuladores.bind(on_press=lambda x: self.cambiar_categoria("SIMULADORES"))

        filtros_bar.add_widget(btn_tareas)
        filtros_bar.add_widget(btn_tests)
        filtros_bar.add_widget(btn_simuladores)
        layout.add_widget(filtros_bar)

        self.lbl_sinc = Label(text="Sincronización lista.", font_size=11, color=(0, 0.9, 0.6, 1), size_hint_y=None, height=20)
        layout.add_widget(self.lbl_sinc)

        scroll = ScrollView()
        self.grid_materias = GridLayout(cols=1, spacing=12, size_hint_y=None)
        self.grid_materias.bind(minimum_height=self.grid_materias.setter('height'))

        scroll.add_widget(self.grid_materias)
        layout.add_widget(scroll)

        self.add_widget(layout)
        self.renderizar_secciones()

    def _update_bg(self, instance, value):
        self.bg_rect.pos = self.pos
        self.bg_rect.size = self.size

    def on_enter(self, *args):
        """Al entrar al dashboard arranca el monitoreo automatico."""
        if not self.monitor_activo:
            self.monitor_activo = True
            threading.Thread(target=self.bucle_monitoreo, daemon=True).start()

    def bucle_monitoreo(self):
        while self.monitor_activo:
            self.proceso_escaneo_moodle(automatico=True)
            for _ in range(INTERVALO_MINUTOS * 60):
                if not self.monitor_activo:
                    return
                time.sleep(1)

    def abrir_enlace_directo(self, url):
        """
        Abre la tarea/test/simulador directo en Chrome, con la sesión ya
        iniciada, para que llegue justo a la actividad sin pedir login.
        """
        previo_texto = self.lbl_sinc.text
        previo_color = tuple(self.lbl_sinc.color)
        self.lbl_sinc.text = "Abriendo la actividad..."
        self.lbl_sinc.color = (1, 0.8, 0, 1)

        def restaurar(dt):
            # Solo restaura si nadie más cambió el mensaje mientras tanto.
            if self.lbl_sinc.text == "Abriendo la actividad...":
                self.lbl_sinc.text = previo_texto
                self.lbl_sinc.color = previo_color

        def trabajo():
            if EN_ANDROID:
                # En el celular se abre el enlace en el navegador del teléfono.
                webbrowser.open(url)
                Clock.schedule_once(restaurar, 1.5)
                return
            asegurar_sesion()
            ok, motivo = abrir_con_sesion(url)
            if ok:
                Clock.schedule_once(restaurar, 1.5)
                return
            registrar_log(f"Fallo abrir con sesión: {motivo} | {url}")

            def aviso(dt):
                self.lbl_sinc.text = f"No pude entrar directo: {motivo}"
                self.lbl_sinc.color = (1, 0.3, 0.4, 1)
            Clock.schedule_once(aviso, 0)
            # Respaldo: acceso automático de Moodle o enlace normal.
            destino = obtener_url_autologin(url) or url
            self._lanzar_navegador(destino)

        threading.Thread(target=trabajo, daemon=True).start()

    def _lanzar_navegador(self, url):
        chrome = encontrar_chrome()
        if chrome:
            try:
                subprocess.Popen([chrome, url])
                return
            except Exception:
                pass
        try:
            webbrowser.get('chrome').open(url)
        except Exception:
            webbrowser.open(url)

    def cambiar_categoria(self, nueva_cat):
        self.categoria_actual = nueva_cat
        self.renderizar_secciones()

    def renderizar_secciones(self):
        self.grid_materias.clear_widgets()
        cat_key_map = {"TAREAS": "tareas", "TESTS": "tests", "SIMULADORES": "simuladores"}
        clave_actividad = cat_key_map.get(self.categoria_actual, "tareas")

        with LOCK_DATOS:
            materias = list(MATERIAS_DETECTADAS)

        if not materias:
            lbl_info = Label(text="Cargando tus materias... la primera vez puede tardar un par de minutos.", font_size=12, color=(0.5, 0.6, 0.7, 1), size_hint_y=None, height=40)
            self.grid_materias.add_widget(lbl_info)
            return

        for mat in materias:
            items_materia = mat.get(clave_actividad, [])
            cant_items = len(items_materia)
            def _alto_item(it):
                return 112 if it.get("info") else 92

            altura_seccion = 55 + (
                sum(_alto_item(i) + 8 for i in items_materia) if items_materia else 100
            )

            seccion_box = TarjetaDios(
                orientation='vertical',
                padding=[14, 10, 14, 10],
                spacing=8,
                size_hint_y=None,
                height=altura_seccion,
                border_color=(0.7, 0.2, 1, 0.4),
                bg_color=(0.07, 0.09, 0.18, 0.95)
            )

            header_materia = Label(
                text=mat['nombre'].upper(),
                font_size=13,
                bold=True,
                color=(0, 0.9, 1, 1),
                halign='left',
                size_hint_y=None,
                height=28
            )
            header_materia.bind(size=header_materia.setter('text_size'))
            seccion_box.add_widget(header_materia)

            if not items_materia:
                lbl_vacio = Label(
                    text=f" Sin {self.categoria_actual.lower()} registradas.",
                    font_size=11,
                    color=(0.4, 0.5, 0.6, 1),
                    halign='left',
                    size_hint_y=None,
                    height=24
                )
                lbl_vacio.bind(size=lbl_vacio.setter('text_size'))
                seccion_box.add_widget(lbl_vacio)
            else:
                for item in items_materia:
                    fila_item = BoxLayout(orientation='horizontal', spacing=10, size_hint_y=None, height=_alto_item(item))

                    texto_completo = f"[b]{item['nombre']}[/b]"
                    if item.get("fechas"):
                        fechas_es = traducir_fechas_a_espanol(item['fechas'])
                        texto_completo += f"\n[color=70c8ff]{fechas_es}[/color]"
                    texto_estado, color_estado = descripcion_estado(item, clave_actividad)
                    texto_completo += f"\n[b][color={color_estado}]{texto_estado}[/color][/b]"
                    info = item.get("info")
                    if info:
                        texto_completo += f"\n[color=ffd27a]Intentos permitidos: {info.get('intentos', '-')}[/color]"
                        texto_completo += f"\n[color=ffd27a]Límite de tiempo: {info.get('tiempo', '-')}[/color]"

                    lbl_item = Label(
                        text=texto_completo,
                        markup=True,
                        font_size=11,
                        color=(1, 1, 1, 1),
                        halign='left',
                        valign='middle'
                    )
                    lbl_item.bind(size=lbl_item.setter('text_size'))

                    btn_ir = BotonDios(
                        text="ABRIR",
                        main_color=(0, 0.8, 0.4, 1),
                        size_hint_x=None,
                        width=85,
                        color=(1, 1, 1, 1),
                        bold=True,
                        font_size=11
                    )
                    btn_ir.bind(on_press=lambda instance, url=item['link']: self.abrir_enlace_directo(url))

                    fila_item.add_widget(lbl_item)
                    fila_item.add_widget(btn_ir)
                    seccion_box.add_widget(fila_item)

            self.grid_materias.add_widget(seccion_box)

    def forzar_escaneo(self, instance):
        if self.escaneando:
            self.lbl_sinc.text = "Ya hay un escaneo en curso..."
            return
        threading.Thread(target=self.proceso_escaneo_moodle, daemon=True).start()

    def proceso_escaneo_moodle(self, automatico=False):
        if self.escaneando:
            return
        self.escaneando = True

        def poner_estado(texto, color):
            def _f(dt):
                self.lbl_sinc.text = texto
                self.lbl_sinc.color = color
            Clock.schedule_once(_f, 0)

        poner_estado(
            "Revisando novedades..." if automatico else "Escaneando plataforma...",
            (1, 0.8, 0, 1),
        )

        ok = False
        cantidad = 0
        error = ""

        def avance(i, total, nombre):
            if i == 0:
                poner_estado(f"Encontré {total} materias, revisando actividades...", (1, 0.8, 0, 1))
            else:
                poner_estado(f"Revisando materias: {i} de {total}...", (1, 0.8, 0, 1))
            Clock.schedule_once(lambda dt: self.renderizar_secciones(), 0)

        try:
            ok = escanear_materias(avance)
            if ok:
                cantidad = revisar_novedades_y_notificar()
        except Exception as e:
            error = str(e)[:80]
        finally:
            self.escaneando = False

        Clock.schedule_once(
            lambda dt: self.actualizar_interfaz_resultado(ok, cantidad, error), 0.1
        )

    def actualizar_interfaz_resultado(self, ok=True, cantidad=0, error=""):
        hora = time.strftime("%H:%M")
        if error:
            self.lbl_sinc.text = f"Error al escanear: {error}"
            self.lbl_sinc.color = (1, 0.3, 0.4, 1)
        elif not ok:
            self.lbl_sinc.text = "No se encontraron materias. Revisa tu sesión."
            self.lbl_sinc.color = (1, 0.3, 0.4, 1)
        else:
            extra = f" ({cantidad} novedades)" if cantidad else ""
            proxima = time.strftime("%H:%M", time.localtime(time.time() + INTERVALO_MINUTOS * 60))
            self.lbl_sinc.text = f"Actualizado a las {hora}{extra}. Próxima actualización automática: {proxima}."
            self.lbl_sinc.color = (0, 1, 0.5, 1)
        self.renderizar_secciones()

    def cerrar_sesion(self, instance):
        self.monitor_activo = False
        SESSION_GLOBAL.cookies.clear()
        self.manager.current = 'login'


class MoodleFabiAppCompleta(App):
    def build(self):
        if EN_ANDROID:
            try:
                from kivy.core.window import Window
                Window.softinput_mode = "below_target"  # el teclado no tapa los campos
                from android.permissions import request_permissions
                request_permissions([
                    "android.permission.INTERNET",
                    "android.permission.POST_NOTIFICATIONS",
                ])
            except Exception:
                pass
        sm = ScreenManager()
        sm.add_widget(LoginScreen(name='login'))
        sm.add_widget(DashboardScreen(name='dashboard'))
        return sm


if __name__ == '__main__':
    MoodleFabiAppCompleta().run()
