# StoryPulse Private

<p align="center">
<img src="http://imgfz.com/i/Y7U30MS.png" title="StoryPulse">
</p>
<br></br>




<p align="center">
<a href="https://github.com/FacuSecX"><img title="Autor" src="https://img.shields.io/badge/Author-Facu%20-blue?style=for-the-badge&logo=github"></a>
<a href=""><img title="Version" src="https://img.shields.io/badge/Version-1.0-red?style=for-the-badge&logo="></a>
</p>

<p align="center">
<a href=""><img title="System" src="https://img.shields.io/badge/Supported%20OS-Linux-orange?style=for-the-badge&logo=linux"></a>
<a href="https://paypal.me/FacuSecX"><img title="Paypal" src="https://img.shields.io/badge/Donate-PayPal-green.svg?style=for-the-badge&logo=paypal"></a>
</p>

<p align="center">
<a href="mailto:facusex@gmail.com"><img title="Correo" src="https://img.shields.io/badge/Correo-facusecX@gmail.com-blueviolet?style=for-the-badge&logo=gmai"></a>
<a href="https://t.me/FacuSecX"><img title="Chat" src="https://img.shields.io/badge/CHAT-TELEGRAM-blue?style=for-thjlje-badge&logo=telegram"></a>

**StoryPulse Private** es un bot de Telegram para realizar consultas automatizadas a Instagram mediante una sesión web autenticada.

Permite monitorear perfiles públicos y también perfiles privados a los que la cuenta de Instagram utilizada para la autenticación tenga acceso, realizar revisiones automáticas, descargar Stories y publicaciones, almacenar el contenido localmente y mantener un registro persistente para evitar duplicados.

Está pensado principalmente para ejecutarse de forma continua en un servidor o VPS.

---

## Funcionalidades

- 👤 Consulta de perfiles públicos y privados accesibles por la cuenta autenticada.
- ⏰ Programación de revisiones automáticas mediante intervalos de tiempo o mediante horarios específicos.
- 📸 Consulta y descarga automática de Stories.
- 🆔 Identificación de cada Story mediante su **Story ID / PK original de Instagram**.
- 🔁 Sistema antirepetición que evita descargar o reenviar Stories previamente procesadas.
- 🕒 Obtención de la fecha y hora original de publicación de cada Story.
- 👥 Gestión de cuentas directamente desde Telegram.
- 🔐 Verificación de acceso antes de agregar perfiles privados.
- 📥 Descarga completa de publicaciones de perfiles.
- 🖼 Soporte para fotografías, videos y carruseles con múltiples archivos.
- 💾 Almacenamiento local de Stories y publicaciones en el servidor.
- 📅 Conservación de metadatos básicos, IDs y fechas de publicación.
- 📋 Programaciones persistentes almacenadas en SQLite.
- 🔔 Notificaciones automáticas cuando se detectan Stories nuevas.
- 🌐 Integración opcional con un panel web para visualizar el contenido almacenado.
- 🩺 Verificación del estado de la sesión web de Instagram desde Telegram.
- 🔄 Actualización manual del estado de sesión.
- 🚫 No abre deliberadamente el visor convencional de Stories ni ejecuta llamadas destinadas específicamente a marcarlas como vistas.

---

## Objetivo

StoryPulse Private está diseñado como una herramienta de monitoreo y organización automática de contenido de Instagram.

Su objetivo es permitir revisar periódicamente determinados perfiles sin necesidad de comprobarlos manualmente, almacenar el contenido nuevo y evitar procesar repetidamente las mismas Stories o publicaciones.

En el caso de los perfiles privados, StoryPulse solamente puede acceder al contenido que sea visible para la cuenta de Instagram utilizada para autenticar la sesión.

Ejemplo:

```text
Cuenta autenticada
        │
        ├── Perfil público
        │      └── Accesible
        │
        ├── Perfil privado seguido
        │      └── Accesible
        │
        └── Perfil privado sin acceso
               └── No accesible
```

StoryPulse no proporciona acceso especial a perfiles privados. La visibilidad depende completamente de los permisos reales de la cuenta autenticada en Instagram.

---

## Funcionamiento de la autenticación

StoryPulse no inicia sesión en Instagram con usuario y contraseña cada vez que realiza una consulta.

En su lugar utiliza una sesión web previamente autenticada que se guarda en:

```text
instagram_state.json
```

La sesión se genera mediante un script de creación de sesión, por ejemplo:

```text
crearsession.py
```

Durante ese proceso se abre un navegador Chromium controlado mediante **Playwright**.

El usuario inicia sesión manualmente en Instagram y completa cualquier CAPTCHA, checkpoint, código de seguridad o verificación que Instagram pueda solicitar.

Una vez que Instagram muestra correctamente la sesión iniciada, el script guarda el estado del navegador en:

```text
instagram_state.json
```

Ese archivo puede contener:

- Cookies de Instagram.
- Cookie `sessionid`.
- Local Storage.
- Estado del navegador relacionado con la sesión.
- Otros datos necesarios para reutilizar la autenticación.

El flujo general es:

```text
Inicio de sesión manual
        ↓
Instagram
        ↓
Playwright + Chromium
        ↓
Generación de instagram_state.json
        ↓
Archivo copiado al VPS
        ↓
StoryPulse carga la sesión
        ↓
Instagram reconoce la cuenta autenticada
```

De esta manera el bot no necesita almacenar la contraseña de Instagram ni iniciar sesión desde cero cada vez que realiza una revisión.

---

## Playwright y Chromium

StoryPulse utiliza **Playwright** para controlar una instancia de **Chromium**.

En el servidor Chromium funciona normalmente en modo:

```text
headless
```

Esto significa que el navegador funciona completamente pero sin mostrar una ventana gráfica.

StoryPulse crea el contexto del navegador utilizando el archivo de sesión:

```python
context = browser.new_context(
    storage_state="instagram_state.json"
)
```

El funcionamiento simplificado es:

```text
instagram_state.json
        ↓
Playwright
        ↓
Chromium Headless
        ↓
Cookies y sesión cargadas
        ↓
Instagram Web autenticado
```

Playwright no es solamente utilizado para simular clics o navegar visualmente por Instagram.

En StoryPulse funciona principalmente como un navegador autenticado capaz de:

- Mantener cookies.
- Mantener la sesión.
- Realizar consultas a Instagram Web.
- Consultar endpoints GraphQL.
- Descargar archivos desde el CDN de Instagram.
- Detectar redirecciones al login.
- Detectar determinados errores HTTP.
- Persistir cambios producidos en la sesión.

Después de determinadas operaciones correctas, StoryPulse puede volver a exportar el estado actualizado del navegador y reemplazar el archivo:

```text
instagram_state.json
```

Esto permite conservar cookies o modificaciones realizadas por Instagram durante el funcionamiento normal.

---

## Obtención de Stories

StoryPulse no obtiene las Stories realizando capturas de pantalla.

Primero identifica el **ID numérico de Instagram** asociado al username consultado.

Ejemplo:

```text
@usuario
    ↓
Instagram User ID
    ↓
123456789
```

Los IDs resueltos pueden almacenarse localmente en caché para evitar tener que obtenerlos nuevamente en cada revisión.

Una vez conocido el User ID, StoryPulse realiza consultas contra los endpoints utilizados por **Instagram Web GraphQL** utilizando la sesión autenticada.

El flujo general es:

```text
Username
   ↓
Instagram User ID
   ↓
Instagram Web GraphQL
   ↓
Respuesta JSON
   ↓
Stories disponibles
```

La respuesta puede contener información como:

```text
Story ID / PK
Fecha de publicación
URL de imagen
URL de video
Tipo de contenido
Información multimedia
```

StoryPulse analiza la respuesta y obtiene cada Story disponible para la sesión autenticada.

Posteriormente extrae la URL real del archivo multimedia.

---

## Descarga del contenido desde Instagram

Una vez encontrada la URL correspondiente a una imagen o video, StoryPulse descarga directamente el archivo desde el **CDN de Instagram**.

El proceso es aproximadamente:

```text
Instagram Web GraphQL
        ↓
Respuesta JSON
        ↓
URL del archivo
        ↓
CDN de Instagram
        ↓
JPG / MP4
        ↓
StoryPulse
        ↓
Servidor
        ↓
Telegram
```

Por lo tanto, el sistema no necesita realizar screenshots del navegador.

Las imágenes y videos se descargan directamente desde las URLs multimedia proporcionadas por Instagram.

---

## Metadatos de Stories

StoryPulse conserva información asociada a cada Story, incluyendo datos como:

```text
username
story_pk
fecha de publicación
tipo de contenido
extensión
imagen o video
```

El identificador más importante es:

```text
story_pk
```

Este valor corresponde al identificador propio de la Story en Instagram.

Gracias a este ID el sistema puede determinar si una Story ya fue procesada anteriormente.

---

## Sistema antirepetición

Antes de procesar una Story, StoryPulse comprueba su ID contra la base de datos SQLite.

El funcionamiento es:

```text
Story encontrada
      ↓
Obtener Story ID
      ↓
¿ID registrado?
   ┌───────┴────────┐
   │                │
   Sí               No
   │                │
Ignorar         Procesar
                    ↓
                 Guardar
                    ↓
             Enviar a Telegram
                    ↓
             Registrar Story ID
```

Esto permite realizar revisiones frecuentes sin que las mismas Stories sean enviadas repetidamente.

La información permanece almacenada incluso si:

- Se reinicia el bot.
- Se reinicia el VPS.
- Se reinicia Python.
- Se reinicia el servicio systemd.

En el flujo normal, una Story se registra como procesada después de completar correctamente su procesamiento.

Esto permite que, ante determinados fallos de Telegram o del servidor, una Story pueda volver a intentarse posteriormente en lugar de quedar perdida.

---

## Stories y visualizaciones

StoryPulse no abre deliberadamente el visor convencional de Stories utilizado normalmente desde la aplicación o la página de Instagram.

Las Stories se consultan a través de los datos proporcionados por Instagram Web y posteriormente el contenido se descarga desde sus URLs multimedia.

El código tampoco ejecuta deliberadamente solicitudes cuyo objetivo específico sea registrar una visualización.

Sin embargo, el funcionamiento interno de Instagram puede cambiar en cualquier momento.

Por este motivo no debe considerarse una garantía permanente de anonimato ni asegurarse que futuras modificaciones de Instagram no cambien este comportamiento.

---

## Publicaciones

StoryPulse también permite descargar publicaciones de perfiles mediante Instagram Web.

Puede procesar:

- Fotografías.
- Videos.
- Carruseles.
- Publicaciones que contienen múltiples imágenes.
- Publicaciones que contienen múltiples archivos multimedia.

Cada publicación puede conservar información como:

```text
username
Post ID
fecha de publicación
cantidad de archivos
tipo de contenido
```

Los archivos son almacenados localmente en el servidor para permitir su conservación y utilización posterior desde Telegram o desde un panel web.

---

## Gestión de perfiles privados

Cuando se intenta agregar una nueva cuenta, StoryPulse verifica previamente si el perfil puede ser consultado por la sesión autenticada.

Ejemplo:

```text
Agregar @usuario
      ↓
Consultar perfil
      ↓
¿Existe?
      ↓
¿Es privado?
      ↓
¿La sesión tiene acceso?
      ↓
Sí → Agregar
No → Rechazar
```

De esta manera se evita agregar perfiles privados que posteriormente no podrían ser consultados.

---

## Estado de sesión

StoryPulse incluye una función de estado que permite comprobar la sesión web utilizada por el bot.

El sistema puede mostrar información similar a:

```text
Sesión web: ✅ cargada
Sesión autenticada como: @usuario
Acceso a Instagram: ✅ normal
Estado de seguridad: ✅ sin challenge detectado
Sesión actualizada hace: 5 horas
Cuentas: 36
Programaciones: 30
```

La comprobación puede detectar situaciones como:

```text
HTTP 400
HTTP 401
HTTP 403
HTTP 429
Redirección al login
Challenge
Checkpoint
CAPTCHA
Sesión inválida
```

El estado puede actualizarse nuevamente desde Telegram mediante el botón correspondiente.

---

# Instalación

## Requisitos

Se recomienda utilizar:

```text
Python 3.11 o superior
Linux Debian / Ubuntu
Playwright
Chromium
SQLite
Telegram Bot
Cuenta de Instagram
Servidor o VPS para funcionamiento 24/7
```

Para pruebas también puede ejecutarse localmente en Windows.

---

## 1. Descargar el proyecto

Clonar el repositorio:

```bash
git clone https://github.com/FacuSecX/StoryPulse-Private/
cd StoryPulse-Private
```

También puede descargarse manualmente desde GitHub.

---

## 2. Instalar Python

En Debian o Ubuntu:

```bash
sudo apt update
sudo apt install -y python3 python3-pip python3-venv
```

Comprobar la instalación:

```bash
python3 --version
```

---

## 3. Crear un entorno virtual

Desde la carpeta del proyecto:

```bash
python3 -m venv venv
```

Activarlo:

```bash
source venv/bin/activate
```

Actualizar `pip`:

```bash
python -m pip install --upgrade pip
```

---

## 4. Instalar dependencias

Si el proyecto contiene:

```text
requirements.txt
```

ejecutar:

```bash
pip install -r requirements.txt
```

---

## 5. Instalar Chromium para Playwright

Ejecutar:

```bash
python -m playwright install --with-deps chromium
```

Esto instalará Chromium y las dependencias necesarias para que Playwright pueda utilizarlo.

Para comprobar la instalación:

```bash
python -m playwright install chromium
```

---

# Generación de la sesión de Instagram

Antes de iniciar StoryPulse debe generarse una sesión autenticada.

Se recomienda realizar este proceso desde una computadora con interfaz gráfica, por ejemplo Windows.

Ejecutar:

```bash
python crearsession.py
```

El script abrirá Chromium mediante Playwright.

Dentro del navegador:

1. Iniciar sesión manualmente en Instagram.
2. Introducir usuario y contraseña.
3. Completar cualquier código de seguridad si Instagram lo solicita.
4. Resolver manualmente cualquier CAPTCHA o checkpoint.
5. Esperar hasta comprobar que el feed de Instagram funciona normalmente.
6. Volver a la consola.
7. Presionar `ENTER` cuando el script lo solicite.

Al finalizar se generará:

```text
instagram_state.json
```

Este archivo contiene la sesión autenticada que posteriormente utilizará StoryPulse.

---

## Importante: seguridad de instagram_state.json

El archivo:

```text
instagram_state.json
```

debe considerarse una credencial sensible.

Una sesión válida puede permitir reutilizar la autenticación de Instagram sin introducir nuevamente la contraseña.

Por este motivo:

- No debe publicarse en GitHub.
- No debe incluirse en releases.
- No debe enviarse públicamente.
- No debe compartirse con terceros.
- No debe aparecer en capturas públicas.
- No debe almacenarse en repositorios públicos.

Debe agregarse a `.gitignore`.

Ejemplo:

```gitignore
.env
instagram_state.json
*.db
__pycache__/
venv/
.pw-browsers/
```

---

# Transferir la sesión al servidor

Una vez generado:

```text
instagram_state.json
```

debe copiarse a la carpeta del proyecto en el servidor.

Ejemplo:

```text
/home/usuario/StoryPulse-Private/instagram_state.json
```

Desde Windows puede utilizarse SCP:

```powershell
scp instagram_state.json usuario@IP_DEL_SERVIDOR:/home/usuario/StoryPulse-Private/
```

En el VPS es recomendable restringir sus permisos:

```bash
chmod 600 instagram_state.json
```

---

# Configuración de Telegram

Crear un bot mediante **BotFather** en Telegram y obtener el token.

Después crear un archivo:

```text
.env
```

Ejemplo:

```env
TELEGRAM_BOT_TOKEN=TOKEN_DEL_BOT
TELEGRAM_CHAT_ID=ID_DE_TELEGRAM

INSTAGRAM_STORAGE_STATE=instagram_state.json

HISTORYS_DIR=/home/usuario/historys

STORYPULSE_PANEL_URL=https://example.com/

STORYPULSE_TIMEZONE=America/Argentina/Buenos_Aires
```

Los valores deben adaptarse a cada instalación.

El archivo `.env` también contiene información sensible y no debe publicarse.

Permisos recomendados:

```bash
chmod 600 .env
```

---

# Primera ejecución

Activar el entorno virtual:

```bash
source venv/bin/activate
```

Ejecutar:

```bash
python bot.py
```

Si todo funciona correctamente el bot debería permanecer activo y responder en Telegram.

Enviar:

```text
/start
```

Para detenerlo manualmente:

```text
CTRL + C
```

---

# Ejecución 24/7 con systemd

Para mantener StoryPulse funcionando permanentemente puede utilizarse `systemd`.

Crear el servicio:

```bash
sudo nano /etc/systemd/system/storypulse.service
```

Ejemplo:

```ini
[Unit]
Description=StoryPulse Private Telegram Bot
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=usuario
WorkingDirectory=/home/usuario/StoryPulse-Private

ExecStart=/home/usuario/StoryPulse-Private/venv/bin/python /home/usuario/StoryPulse-Private/bot.py

Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

Guardar el archivo y ejecutar:

```bash
sudo systemctl daemon-reload
sudo systemctl enable storypulse.service
sudo systemctl start storypulse.service
```

Comprobar:

```bash
sudo systemctl status storypulse.service
```

Si funciona correctamente debería aparecer:

```text
Active: active (running)
```

---

# Reiniciar StoryPulse

Después de modificar archivos:

```bash
sudo systemctl restart storypulse.service
```

---

# Detener StoryPulse

```bash
sudo systemctl stop storypulse.service
```

---

# Iniciar StoryPulse

```bash
sudo systemctl start storypulse.service
```

---

# Ver logs

Últimas líneas:

```bash
sudo journalctl -u storypulse.service -n 100 --no-pager
```

Logs en tiempo real:

```bash
sudo journalctl -u storypulse.service -f
```

---

# Actualización del proyecto

Cuando se modifica el código puede reemplazarse únicamente el archivo correspondiente.

Por ejemplo:

```text
bot.py
history.py
publicaciones.py
database.py
```

Después reiniciar el servicio:

```bash
sudo systemctl restart storypulse.service
```

Y comprobar:

```bash
sudo systemctl status storypulse.service
```

---

# Renovación de la sesión de Instagram

Una sesión de Instagram puede dejar de funcionar por diferentes motivos:

- Expiración de cookies.
- Cambio de contraseña.
- Cierre manual de sesiones.
- CAPTCHA.
- Checkpoint de seguridad.
- Challenge.
- Actividad considerada inusual.
- Invalidación de sesión por Instagram.
- Cambios internos de la plataforma.

Cuando ocurre alguno de estos casos pueden aparecer errores como:

```text
HTTP 400
HTTP 401
HTTP 403
HTTP 429
Redirección al login
Challenge
Checkpoint
Session invalid
```

En ese caso debe generarse nuevamente la sesión.

Ejecutar desde una computadora con navegador:

```bash
python crearsession.py
```

Iniciar sesión nuevamente y completar cualquier verificación manual.

Después reemplazar en el servidor:

```text
instagram_state.json
```

y reiniciar StoryPulse:

```bash
sudo systemctl restart storypulse.service
```

---

# Uso responsable

Instagram utiliza diferentes mecanismos automáticos para detectar actividad inusual.

Entre ellos pueden encontrarse:

```text
Rate Limits
CAPTCHA
Checkpoint
Challenge
Bloqueos temporales
Invalidación de sesiones
```

El uso de Playwright y Chromium no elimina estos mecanismos ni garantiza que una cuenta nunca reciba una verificación.

Por este motivo se recomienda:

- Utilizar intervalos de consulta razonables.
- Evitar consultas excesivamente frecuentes.
- Evitar ejecutar muchas consultas simultáneas.
- Distribuir las programaciones a lo largo del tiempo.
- Mantener una sesión estable.
- Evitar regenerar sesiones innecesariamente.
- No realizar ciclos agresivos de consultas.
- Respetar la privacidad de terceros.
- Respetar las condiciones y políticas aplicables de Instagram.

---

# Seguridad

Los principales archivos sensibles del proyecto son:

```text
.env
instagram_state.json
```

Estos archivos nunca deberían publicarse.

Ejemplo recomendado de `.gitignore`:

```gitignore
# Credenciales
.env

# Sesión de Instagram
instagram_state.json

# Bases de datos locales
*.db
*.sqlite
*.sqlite3

# Python
__pycache__/
*.pyc
*.pyo

# Entornos virtuales
venv/
.venv/

# Playwright
.pw-browsers/

# Logs
*.log
```

También se recomienda restringir los permisos en Linux:

```bash
chmod 600 .env
chmod 600 instagram_state.json
```

---

# Arquitectura del proyecto

El funcionamiento general puede resumirse de la siguiente manera:

```text
┌─────────────────────────┐
│        Telegram         │
└────────────┬────────────┘
             │
             ▼
┌─────────────────────────┐
│         bot.py          │
│                         │
│ Menús                   │
│ Programaciones          │
│ Gestión de cuentas      │
│ Antirepetición          │
│ Notificaciones          │
└────────────┬────────────┘
             │
       ┌─────┴─────┐
       │           │
       ▼           ▼
┌────────────┐ ┌──────────────────┐
│ history.py │ │ publicaciones.py │
└─────┬──────┘ └────────┬─────────┘
      │                 │
      └────────┬────────┘
               │
               ▼
┌─────────────────────────┐
│  Playwright + Chromium  │
│        Headless         │
└────────────┬────────────┘
             │
             ▼
┌─────────────────────────┐
│  instagram_state.json   │
│                         │
│ Sesión web autenticada  │
└────────────┬────────────┘
             │
             ▼
┌─────────────────────────┐
│      Instagram Web      │
│                         │
│ GraphQL                 │
│ CDN                     │
│ Perfil                  │
│ Stories                 │
│ Publicaciones           │
└─────────────────────────┘
```

---

# Base de datos

StoryPulse utiliza SQLite para conservar información persistente.

Entre los datos que pueden almacenarse se encuentran:

```text
Stories procesadas
Story IDs
Programaciones
Estados de programación
Mensajes registrados
Información necesaria para antirepetición
```

Esto permite que el sistema conserve su estado incluso después de reiniciar el proceso o el servidor.

---

# Archivos principales

Una instalación típica puede contener:

```text
StoryPulse-Private/
│
├── bot.py
├── history.py
├── publicaciones.py
├── database.py
├── crearsession.py
├── requirements.txt
├── cuentas.json
├── user_ids_cache.json
├── instagram_state.json
├── bot_historias.db
└── .env
```

Algunos archivos pueden generarse automáticamente durante la ejecución.

---

# Limitaciones

StoryPulse utiliza mecanismos internos de Instagram Web.

No utiliza la API oficial de Instagram para la consulta de Stories y publicaciones.

Instagram puede modificar sin previo aviso:

- Endpoints.
- GraphQL.
- Query hashes.
- Cookies.
- Estructuras JSON.
- Sistemas de autenticación.
- Sistemas antiautomatización.
- URLs multimedia.
- Políticas de acceso.

Una modificación importante de Instagram puede requerir actualizar StoryPulse.

---

# Aviso

Este proyecto debe utilizarse de forma responsable y únicamente sobre contenido al que la cuenta autenticada tenga acceso legítimo.

El acceso a perfiles privados depende exclusivamente de los permisos de la cuenta utilizada para iniciar sesión.

StoryPulse no evita controles de privacidad de Instagram y no proporciona acceso a contenido que la cuenta autenticada no pueda visualizar normalmente.

El comportamiento relacionado con visualizaciones de Stories, endpoints internos y mecanismos de Instagram puede cambiar en cualquier momento.


## Instalacion en servidores

En el servidor ejecuta

```text
chmod +x server_install.sh
sudo ./server_install.sh
```


