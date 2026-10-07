# StoryPulse Private v2.0

<p align="center">
<img src="http://imgfz.com/i/QnmALpy.png" title="StoryPulse">
</p>
<br></br>




<p align="center">
<a href="https://github.com/FacuSecX"><img title="Autor" src="https://img.shields.io/badge/Author-Facu%20-blue?style=for-the-badge&logo=github"></a>
<a href=""><img title="Version" src="https://img.shields.io/badge/Version-2.0-red?style=for-the-badge&logo="></a>
</p>

<p align="center">
<a href=""><img title="System" src="https://img.shields.io/badge/Supported%20OS-Linux-orange?style=for-the-badge&logo=linux"></a>
<a href="https://paypal.me/FacuSecX"><img title="Paypal" src="https://img.shields.io/badge/Donate-PayPal-green.svg?style=for-the-badge&logo=paypal"></a>
</p>

<p align="center">
<a href="mailto:facusex@gmail.com"><img title="Correo" src="https://img.shields.io/badge/Correo-facusecX@gmail.com-blueviolet?style=for-the-badge&logo=gmai"></a>
<a href="https://t.me/FacuSecX"><img title="Chat" src="https://img.shields.io/badge/CHAT-TELEGRAM-blue?style=for-thjlje-badge&logo=telegram"></a>
</p>

**StoryPulse Private** es un bot de Telegram para realizar consultas automatizadas a Instagram mediante sesiones web independientes y previamente autenticadas.

Permite monitorear perfiles públicos y también perfiles privados a los que la cuenta de Instagram utilizada para la autenticación tenga acceso, realizar revisiones automáticas, descargar Stories, publicaciones e historias destacadas, almacenar el contenido localmente y mantener un registro persistente para evitar duplicados.

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
- 🩺 Verificación del feed autenticado de cada sesión de Instagram desde Telegram.
- 🔄 Comprobación manual del estado actual de las sesiones.
- 🚫 No abre deliberadamente el visor convencional de Stories ni ejecuta llamadas destinadas específicamente a marcarlas como vistas.

---

## Funcionalidades 2.0

- 🔐 **Sistema multi-autenticación:** varias cuentas de Instagram con sus propias sesiones; si falla una consulta, se intenta con otra sesión habilitada que tenga acceso registrado al mismo perfil.
- 🔄 **Programaciones variables entre sesiones:** elección de una o varias sesiones con acceso confirmado y rotación entre ellas en las revisiones automáticas, tanto por intervalos como por horarios específicos.
- ✨ **Descarga de Highlights o historias destacadas:** imágenes y videos organizados por perfil y carrusel, con progreso y resumen desde Telegram.
- 🎯 **Sesión preferida para cada cuenta monitoreada:** selección desde Telegram; la preferencia manual se conserva aunque una consulta necesite una alternativa autorizada.
- 🛡️ **Antirepetición mejorada:** control persistente por IDs, compartido entre sesiones, filtrado de elementos repetidos y protección frente a revisiones manuales y automáticas simultáneas.
- 🧹 **Reinicio selectivo de la antirepetición:** por perfil y por tipo de contenido —Stories, publicaciones o destacadas—, o de todos sus registros, con confirmación desde Telegram.
- 🧩 **Actualizar sesiones:** comprobación de una sesión nueva contra los perfiles agregados para incorporarla como respaldo donde tenga acceso, conservando las preferencias y el avance de la comprobación.
- ⭐ **Cuentas favoritas:** perfiles marcados con una estrella y botones destacados para encontrarlos fácilmente en los menús.
- 💾 **Sesiones independientes y persistentes:** cada cuenta conserva su archivo de autenticación, su estado y sus asociaciones entre reinicios.

---

# Objetivo v2.0

El sistema multi-autenticación está pensado para que, cuando falla la consulta de un perfil, pueda intentarse desde varias cuentas con acceso confirmado, reduciendo las revisiones manuales de las sesiones.

La versión 2.0 mejora la continuidad de las consultas, permite elegir qué sesión utilizar para cada perfil y distribuir las revisiones automáticas entre sesiones autorizadas. Además, incorpora el archivo de historias destacadas y refuerza la antirepetición para conservar contenido de forma más ordenada y evitar envíos repetidos al cambiar de cuenta.

StoryPulse Private está diseñado como una herramienta de monitoreo y organización automática de contenido de Instagram.

Su objetivo es permitir revisar periódicamente determinados perfiles sin necesidad de comprobarlos manualmente, almacenar el contenido nuevo y evitar procesar repetidamente las mismas Stories o publicaciones.

En el caso de los perfiles privados, cada consulta sólo puede acceder al contenido visible para la cuenta de Instagram de la sesión utilizada. Tener varias sesiones no concede permisos adicionales.

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

## Sistema multi-autenticación

StoryPulse no inicia sesión con usuario y contraseña cada vez que realiza una consulta. Reutiliza sesiones web autenticadas manualmente y mantiene sus cookies separadas por cuenta.

### Cómo se generan las sesiones

Desde una computadora con interfaz gráfica se ejecuta `crear_session.py`. Para registrar cuentas independientes:

```bash
python crear_session.py
python crear_session.py --cuenta cuenta_respaldo
```

El primer comando crea la sesión `principal` y el segundo agrega otra cuenta independiente. `cuenta_respaldo` es un ejemplo: debe reemplazarse por el username de la cuenta que iniciará sesión en Instagram, no por el perfil que se va a monitorear.

El script abre un navegador mediante **Playwright**: Microsoft Edge por defecto, con alternativa Chromium. También se puede elegir Chrome o Chromium con `--navegador`. El inicio de sesión, el doble factor, los CAPTCHA y los checkpoints se completan manualmente. La exportación se realiza automáticamente cuando detecta una sesión autenticada estable; no hace falta presionar `ENTER`.

### Dónde se guardan

```text
instagram_state.json             # Sesión principal
sesiones_instagram/
└── cuenta_respaldo.json

sesiones_instagram.json
estado_sesiones_instagram.json
perfiles_instagram/
```

- `sesiones_instagram/<cuenta>.json`: estado autenticado de cada cuenta, incluidas sus cookies. Si la cuenta ya está registrada, el creador renueva la ruta configurada.
- `sesiones_instagram.json`: configuración de sesiones, sus identificadores, archivos y estado habilitado. El creador registra cada sesión automáticamente.
- `estado_sesiones_instagram.json`: preferencias por perfil, sesiones con acceso confirmado y resultados de consultas y actualizaciones. Se genera durante el uso del bot.
- `perfiles_instagram/`: perfiles locales del navegador utilizados para crear o renovar las sesiones. Los de Chrome y Chromium usan sufijos propios; no se transfieren al VPS.

Se conserva compatibilidad con la sesión anterior: sin configuración multi-sesión, el bot utiliza la entrada `principal` y `instagram_state.json`, o la ruta indicada en `INSTAGRAM_STORAGE_STATE`. Ejecutar el creador sin `--cuenta` genera o renueva esa sesión principal y usa un perfil local independiente.

Las rutas de configuración y registro pueden ajustarse mediante `INSTAGRAM_SESSIONS_CONFIG` e `INSTAGRAM_SESSIONS_REGISTRY` en `.env`.

### Cómo se elige una sesión

Al agregar un perfil nuevo, el bot comprueba las sesiones disponibles y guarda cuáles confirmaron acceso. En las consultas normales empieza por la preferida y, si falla, utiliza únicamente alternativas habilitadas con acceso registrado al mismo perfil.

Desde **Gestionar cuentas → Cambiar sesión preferida** puede elegirse la cuenta de Instagram para cada perfil. Si todavía no tiene acceso registrado, se verifica esa pareja antes de guardar el cambio. La preferida elegida manualmente se conserva aunque una revisión utilice otra cuenta.

Después de copiar una sesión nueva al servidor, **Gestionar cuentas → Actualizar sesiones** permite comprobarla contra los perfiles agregados y añadirla como respaldo donde confirme acceso, sin cambiar las preferencias. El avance se conserva para continuar una comprobación pendiente.

En una **programación variable**, se comprueba qué sesiones tienen acceso y se elige una o varias para rotar entre ellas. La rotación cambia la cuenta utilizada en cada ejecución; los intervalos y horarios se configuran por separado. La preferida normal del perfil permanece guardada.

Una consulta correcta con cero Stories se informa como «sin historias»; no se interpreta como un fallo de autenticación ni obliga a probar otras cuentas.

```text
Inicio de sesión manual por cuenta
        ↓
Playwright + navegador visible
        ↓
JSON independiente por sesión + configuración
        ↓
Transferencia privada al VPS
        ↓
Perfil → sesión preferida / sesiones autorizadas
        ↓
Consulta a Instagram con la sesión elegida
```

El bot no necesita guardar las contraseñas de Instagram. Los JSON de sesión y los perfiles del navegador sí contienen credenciales reutilizables y deben mantenerse privados.

---

## Playwright y Chromium

StoryPulse utiliza **Playwright** para controlar una instancia de **Chromium**.

En el servidor Chromium funciona normalmente en modo:

```text
headless
```

Esto significa que el navegador funciona completamente pero sin mostrar una ventana gráfica.

StoryPulse crea el contexto del navegador utilizando el archivo de la sesión seleccionada. De forma simplificada:

```python
context = browser.new_context(
    storage_state=str(obtener_ruta_sesion())
)
```

El funcionamiento simplificado es:

```text
JSON de la sesión seleccionada
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

Después de determinadas operaciones correctas, StoryPulse puede volver a exportar el estado actualizado del navegador y reemplazar únicamente el JSON de la sesión que realizó la consulta.

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

### Mejoras de antirepetición en v2.0

- La identificación se realiza por el contenido y el perfil, sin depender de la cuenta de Instagram usada para consultar. Cambiar o rotar de sesión conserva los registros anteriores.
- Las revisiones manuales y automáticas comparten una sección de procesamiento protegida para impedir que ambas envíen simultáneamente la misma Story.
- Se filtran los IDs repetidos dentro de una misma respuesta de Instagram.
- Las historias destacadas tienen registros propios por carrusel y Story ID; las publicaciones conservan sus propios IDs e información de sincronización.
- Desde **Gestionar cuentas → Reiniciar antirepetición** puede elegirse un perfil y limpiar Stories, publicaciones, destacadas o todos sus registros. Se solicita confirmación antes de aplicar el cambio.

El reinicio selectivo no elimina cuentas, programaciones, sesiones ni archivos descargados. Al limpiar publicaciones también se reinicia su avance de sincronización para permitir recorrer nuevamente el historial. Limpiar registros permite reprocesar contenido que continúe disponible; debe utilizarse de forma deliberada.

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

## Highlights o historias destacadas

Desde **Historias destacadas** en el menú principal se elige un perfil agregado para descargar sus carruseles de destacadas. La consulta utiliza la sesión preferida y, si hace falta, otra sesión con acceso registrado al perfil.

Las fotos y videos se conservan en el servidor, organizados por perfil y nombre del carrusel:

```text
HISTORYS_DIR/
└── perfil_objetivo/
    └── Higlights/
        ├── viajes/
        └── recuerdos/
```

`Higlights` es el nombre de carpeta utilizado por el código. Los títulos de los carruseles se normalizan para obtener nombres de carpeta válidos.

Telegram muestra el progreso, que se actualiza aproximadamente cada diez segundos, y un resumen de archivos nuevos y ya guardados. Esta función descarga al almacenamiento local; no envía todos los archivos multimedia al chat.

La antirepetición conserva el ID del carrusel y el ID original de cada Story. Repetir la consulta o utilizar otra sesión mantiene el registro del contenido ya descargado.

---

## Gestión de perfiles privados

Cuando se intenta agregar una nueva cuenta, StoryPulse verifica previamente cuáles de sus sesiones autenticadas pueden consultar el perfil.

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
¿Alguna sesión confirma acceso?
      ↓
Sí → Agregar
No → Rechazar
```

De esta manera se evita agregar perfiles privados que posteriormente no podrían ser consultados.

---

## Estado de las sesiones

La opción **Estado** de Telegram comprueba el feed autenticado de cada sesión habilitada. Muestra el progreso, el resultado actual de cada cuenta y un resumen de perfiles y programaciones.

La comprobación utiliza Instagram y se realiza de forma secuencial, esperando su turno si hay otra consulta en curso. No descarga Stories ni modifica la antirepetición, las preferencias o el historial de errores de las consultas.

El resultado corresponde al momento de la comprobación del feed: no garantiza acceso a todos los perfiles ni que una consulta posterior vaya a funcionar.

Pueden aparecer situaciones como:

```text
HTTP 400 / HTTP 401 / HTTP 403 / HTTP 429
Redirección al login
Challenge / Checkpoint / CAPTCHA
Archivo ausente o inválido
Sesión vencida
```

Para revisar únicamente archivos, cookies y errores registrados, sin conectar a Instagram, puede utilizarse:

```bash
python probar_sesion_vps.py --local
```

Este diagnóstico se identifica como **sin comprobación remota**. La existencia de cookies no garantiza que Instagram acepte la siguiente consulta.

**Actualizar sesiones** es una función distinta: comprueba expresamente el acceso de una sesión nueva o pendiente a los perfiles agregados para incorporarla como respaldo donde corresponda.

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

# Generación de las sesiones de Instagram

Antes de iniciar StoryPulse deben existir los JSON de las sesiones habilitadas. Se recomienda generarlos desde una computadora con interfaz gráfica.

Para crear o renovar la sesión principal compatible con la versión anterior:

```bash
python crear_session.py
```

Para agregar una segunda cuenta independiente, reemplazar el username de ejemplo:

```bash
python crear_session.py --cuenta cuenta_respaldo
```

El creador abre Edge por defecto y recurre a Chromium si no puede iniciarlo. Puede elegirse otro navegador:

```bash
python crear_session.py --cuenta cuenta_respaldo --navegador chromium
python crear_session.py --cuenta cuenta_respaldo --navegador chrome
```

Dentro del navegador:

1. Iniciar sesión manualmente con la cuenta que se está registrando.
2. Completar el doble factor, los códigos de seguridad, CAPTCHA o checkpoints solicitados.
3. Esperar hasta que Instagram funcione con la sesión iniciada.
4. Mantener el navegador abierto mientras el script confirma el estado y exporta el JSON automáticamente.
5. Comprobar en la consola que aparece «SESIÓN EXPORTADA».

No hace falta presionar `ENTER`. El tiempo de espera inicial es de diez minutos y puede ampliarse con `--espera 1200`.

El resultado predeterminado es:

```text
instagram_state.json                         # Principal
sesiones_instagram/cuenta_respaldo.json       # Cuenta adicional
sesiones_instagram.json                      # Registro de configuración
```

Si una cuenta ya está registrada, se renueva el archivo indicado en su configuración. Los perfiles del navegador quedan localmente en `perfil_instagram_playwright/` o `perfiles_instagram/`, con sufijos según el navegador cuando corresponda.

El creador actualiza `sesiones_instagram.json` automáticamente. El script `crearsession.py` se conserva como alias compatible del creador nuevo y acepta los mismos argumentos. El archivo de ejemplo `sesiones_instagram.example.json` sirve como referencia para configuración manual; contiene placeholders y no sesiones utilizables. Su campo `version: 1` identifica el formato de configuración, no la versión del proyecto.

La entrada `principal` se conserva por compatibilidad. Debe tener un archivo válido o estar deshabilitada explícitamente si la instalación sólo va a utilizar cuentas con nombre.

---

## Seguridad de los archivos de sesión

Todos los JSON de estado autenticado, incluido `instagram_state.json` y los de `sesiones_instagram/`, son credenciales sensibles. Pueden contener cookies, `sessionid`, Local Storage y otros datos que permiten reutilizar la autenticación.

Los perfiles del navegador también deben mantenerse privados. La configuración y el registro locales contienen usernames, asociaciones y datos de operación.

Por este motivo:

- No publicar sesiones, perfiles, configuraciones activas ni registros en GitHub.
- No incluirlos en releases, ZIP públicos, capturas o mensajes públicos.
- Compartir únicamente ejemplos con valores ficticios.
- Mantenerlos excluidos mediante `.gitignore`.

Ejemplo:

```gitignore
.env
instagram_state*.json
sesiones_instagram/
sesiones_instagram.json
estado_sesiones_instagram.json
perfiles_instagram/
perfil_instagram_playwright*/
renovar_session_vps.local.json
.renovacion_backup/
*.db
__pycache__/
venv/
.pw-browsers/
```

---

# Transferir las sesiones al servidor

Copiar la configuración `sesiones_instagram.json` y los JSON de todas las sesiones habilitadas a la carpeta del proyecto en el VPS, conservando las rutas relativas.

Ejemplo:

```text
/home/usuario/StoryPulse-Private/
├── instagram_state.json
├── sesiones_instagram.json
└── sesiones_instagram/
    └── cuenta_respaldo.json
```

Desde Windows puede utilizarse SCP. Reemplazar `usuario` e `IP_DEL_SERVIDOR` por los datos de la propia instalación:

```powershell
scp instagram_state.json sesiones_instagram.json usuario@IP_DEL_SERVIDOR:/home/usuario/StoryPulse-Private/
scp -r sesiones_instagram usuario@IP_DEL_SERVIDOR:/home/usuario/StoryPulse-Private/
```

No copiar los perfiles de navegador al VPS. `estado_sesiones_instagram.json` se genera allí y debe conservarse al actualizar el código; no se reemplaza con el registro de otra instalación.

En el servidor, los archivos deben pertenecer al usuario que ejecuta el bot. Se recomienda restringir permisos:

```bash
chmod 600 instagram_state.json sesiones_instagram.json
chmod 700 sesiones_instagram
chmod 600 sesiones_instagram/*.json
```

Al renovar archivos de una instalación activa, detener el servicio durante el reemplazo para evitar que una consulta en curso sobrescriba el JSON recién transferido. Conservar previamente una copia privada de la sesión anterior y reiniciar después de verificar los archivos.

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
INSTAGRAM_SESSIONS_CONFIG=sesiones_instagram.json
INSTAGRAM_SESSIONS_REGISTRY=estado_sesiones_instagram.json

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

Al actualizar a v2.0 deben reemplazarse todos los archivos incluidos en el paquete de actualización y añadirse los módulos nuevos. En cambios posteriores puede reemplazarse únicamente el archivo correspondiente.

Por ejemplo:

```text
bot.py
history.py
publicaciones.py
highlights.py
instagram_sessions.py
database.py
crear_session.py
```

Conservar .env, las sesiones, sus configuraciones activas, cuentas.json, los registros locales y la base de datos. Las tablas nuevas se crean o migran automáticamente al iniciar el bot.

Después reiniciar el servicio:

```bash
sudo systemctl restart storypulse.service
```

Y comprobar:

```bash
sudo systemctl status storypulse.service
```

---

# Renovación de una sesión de Instagram

Una sesión puede dejar de funcionar por vencimiento, cierre de sesión, cambios de contraseña, verificación de seguridad o invalidación por Instagram.

Renovar únicamente la cuenta afectada desde una computadora con navegador:

```bash
python crear_session.py --cuenta cuenta_respaldo
```

Para la sesión principal:

```bash
python crear_session.py
```

Completar manualmente las verificaciones y esperar la exportación. Copiar el JSON renovado a su ruta configurada en el VPS, conservando las sesiones y asociaciones de las demás cuentas.

Si es necesario renovar un archivo que utiliza un alias, el creador permite indicar la ruta exacta con `--archivo-sesion`. No se debe cambiar la identidad de la cuenta asociada a un JSON existente.

El programa vuelve a considerar una sesión cuyo JSON haya cambiado. Si continúa apareciendo un error de login o CAPTCHA, comprobar el inicio de sesión manual antes de exportar nuevamente.

## Renovación asistida en Windows

`RENOVAR_SESSION.bat` y `renovar_session_vps.py` permiten elegir un JSON de `sesiones_instagram/`, renovarlo con su perfil local y transferir únicamente ese archivo al VPS mediante WinSCP.

Copiar `renovar_session_vps.config.ejemplo.json` a `renovar_session_vps.local.json` y completar los datos de la propia instalación. La configuración privada, la llave PPK y los backups locales nunca se publican.

La herramienta requiere Playwright, WinSCP, la llave y sus rutas configuradas. La entrada principal, guardada fuera de `sesiones_instagram/`, se renueva con el creador directamente.

El proceso comprueba el JSON y la identidad de la sesión, reemplaza el archivo remoto con el servicio detenido y verifica el reinicio. Si falla, conserva o intenta restaurar la sesión anterior según la etapa. Un fallo de transferencia requiere revisar el estado del servidor antes de volver a ejecutar.

Después de una transferencia manual, reiniciar el servicio de la propia instalación:

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

Los archivos privados de una instalación incluyen:

```text
.env
instagram_state.json
sesiones_instagram/*.json
sesiones_instagram.json
estado_sesiones_instagram.json
perfiles_instagram/
perfil_instagram_playwright*/
cuentas.json
user_ids_cache.json
bot_historias.db
renovar_session_vps.local.json
.renovacion_backup/
Llaves privadas SSH / PPK
Logs y contenido descargado
```

Los ejemplos publicados contienen placeholders. Cada instalación debe crear sus archivos privados localmente.

Ejemplo recomendado de `.gitignore`:

```gitignore
# Credenciales y configuración privada
.env
.env.*
!.env.example
*.ppk
*.pem
*.key
renovar_session_vps.local.json
.renovacion_backup/

# Sesiones, asociaciones y perfiles locales
instagram_state*.json
sesiones_instagram/
sesiones_instagram.json
estado_sesiones_instagram.json
perfiles_instagram/
perfil_instagram_playwright*/

# Perfiles consultados y caché
cuentas.json
user_ids_cache.json

# Bases de datos locales
*.db
*.db-*
*.sqlite
*.sqlite-*
*.sqlite3
*.sqlite3-*

# Python y entornos virtuales
__pycache__/
*.pyc
*.pyo
venv/
.venv/

# Navegadores, logs y contenido descargado
.pw-browsers/
*.log
historys/
```

También se recomienda restringir los permisos en Linux:

```bash
chmod 600 .env instagram_state.json sesiones_instagram.json
chmod 700 sesiones_instagram
chmod 600 sesiones_instagram/*.json
```

Aplicar `chmod 600` a `estado_sesiones_instagram.json` cuando exista. Las rutas personalizadas también deben protegerse y excluirse del repositorio.

`.gitignore` no elimina archivos que ya estén versionados: si una sesión o token se publicó anteriormente, debe retirarse del historial y renovarse la credencial.

---

# Arquitectura del proyecto

El funcionamiento general puede resumirse de la siguiente manera:

```text
Telegram
   │
   ▼
bot.py ────────────────────────────────► database.py / SQLite
   │                                      │
   │                                      ├── Programaciones y rotación
   │                                      └── IDs de antirepetición
   ▼
instagram_sessions.py
   │
   ├── Configuración de sesiones
   ├── Preferida y accesos por perfil
   └── JSON independiente por cuenta
   │
   ▼
history.py / publicaciones.py / highlights.py
   │
   ▼
Playwright + Chromium Headless
   │
   ▼
Instagram Web / GraphQL / CDN
   │
   ▼
Contenido y metadatos en HISTORYS_DIR
   │
   ├── Stories → Telegram según la revisión y sus ajustes
   └── Publicaciones y destacadas → archivos locales y resumen
```

`crear_session.py` genera o renueva las sesiones en una computadora con navegador visible. La configuración de sesiones y sus JSON se transfieren privadamente al servidor; el estado de preferencias y accesos se conserva allí.

---

# Base de datos

StoryPulse utiliza SQLite para conservar información persistente.

Entre los datos que pueden almacenarse se encuentran:

```text
Stories procesadas
Story IDs
IDs de publicaciones y destacadas
Avance de sincronización de publicaciones
Programaciones
Sesiones elegidas y último turno de rotación
Estados de programación
Mensajes registrados
Información necesaria para antirepetición
```

Esto permite que el sistema conserve su estado incluso después de reiniciar el proceso o el servidor.

---

# Archivos principales

Código y ejemplos publicados:

```text
StoryPulse-Private/
├── bot.py
├── history.py
├── publicaciones.py
├── highlights.py
├── instagram_sessions.py
├── database.py
├── crear_session.py
├── crearsession.py
├── probar_sesion_vps.py
├── renovar_session_vps.py
├── RENOVAR_SESSION.bat
├── renovar_session_vps.config.ejemplo.json
├── sesiones_instagram.example.json
├── requirements.txt
├── server_install.sh
├── README.md
├── .env.example
└── .gitignore
```

Archivos privados creados o configurados en cada instalación:

```text
.env
cuentas.json
user_ids_cache.json
instagram_state.json
sesiones_instagram.json
sesiones_instagram/
estado_sesiones_instagram.json
perfiles_instagram/
perfil_instagram_playwright*/
bot_historias.db
renovar_session_vps.local.json
```

Algunos archivos se generan automáticamente durante la ejecución. Los datos privados no forman parte del repositorio ni del paquete de actualización.

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


## Instalación en servidores

En el servidor ejecuta

```text
chmod +x server_install.sh
sudo ./server_install.sh
```
