# Estándar de mis proyectos

> **Para el agente de IA:** este documento es **obligatorio** en todos los proyectos de Pol ([github.com/polreig](https://github.com/polreig)).
> Léelo antes de cualquier cambio y aplícalo siempre, aunque no te lo recuerde.
> Es **idéntico en todos los repositorios**. Si lo mejoras en uno, avisa para copiarlo a los demás y sube la versión.
>
> Versión 2 · 2026-11-10 (los cambios respecto a la anterior están al final)

## 1. Cómo uso mis proyectos

- Las apps las quiero **usar**, no mantener a mano. Lo importante es que funcionen y que sepa instalarlas sin ayuda.
- A veces programo en el ordenador del trabajo, pero **uso las apps en mi portátil personal (Windows 11)**. El camino siempre es: **GitHub → descargar en casa → instalar → usar**.
- **Instalación:** con doble clic.
- **Uso:** desde una ventana, una web o un acceso directo. La consola es solo para desarrollar.
- **Idioma:** español en la interfaz, los mensajes, el README, los comentarios, los nombres en el código y los commits.

## 2. Organización

### Dónde viven los proyectos

- **Una carpeta para todos los proyectos** y, dentro, **un repo por carpeta, en un solo nivel**: `C:\Users\<tu usuario>\Proyectos\<repo>`.
  - No en Descargas: es lo que ordena el organizador.
  - No en Documentos: el organizador la analiza para sugerir carpetas.
  - Nunca una carpeta dentro de otra igual, como pasa al descomprimir un ZIP en una carpeta con el mismo nombre.
- **La carpeta se llama igual que el repo en GitHub**, en **minúsculas y con guiones**: `organizador-archivos`, no `OrganizadorArchivos` ni `organizador-archivos-main`.
- **Los datos personales de entrada** (exportaciones, copias…) van en la carpeta de datos del proyecto, nunca sueltos al lado del repo.

### Estructura de cada repositorio

Todos los proyectos tienen esta forma (adapta solo lo que no aplique, y explica por qué en `AGENTS.md`):

```
mi-proyecto/
  README.md               para usar la app (plantilla del punto 4)
  CHANGELOG.md            qué cambia en cada versión, en lenguaje de usuario
  AGENTS.md               lo propio de este proyecto (plantilla del punto 8)
  ESTANDAR_PROYECTOS.md   este archivo, idéntico en todos los repos
  CLAUDE.md               solo dos líneas:  @ESTANDAR_PROYECTOS.md  y  @AGENTS.md
  Instalar.bat            doble clic para instalar (llama a scripts\install.ps1)
  Desinstalar.bat         quita lo instalado (llama a scripts\uninstall.ps1)
  <App>.bat               opcional: abrir la app sin acceso directo
  config.example.*        configuración de ejemplo, comentada (la real está en .gitignore)
  requirements.txt        dependencias (si las hay)
  <paquete>/              el código (un paquete con el nombre del proyecto)
  scripts/                install.ps1, uninstall.ps1 y herramientas de desarrollo
  tests/                  solo pruebas
  docs/capturas/          imágenes del README
  datos/                  lo que genera la app (en .gitignore, nunca se sube)
  .gitignore              config real, datos/, .env, cachés, entornos virtuales
  .gitattributes          *.bat y *.ps1 con saltos de línea CRLF
```

- **La raíz es para el usuario:** solo lo que se lee (`.md`), lo que se pulsa (`.bat`) y la configuración de ejemplo. Todo lo demás va en una carpeta.
- **Código:** dentro de **un paquete** (`<paquete>/`), no como archivos `.py` sueltos en la raíz. Los puntos de entrada se ejecutan como módulo: `python -m <paquete>` o `python -m <paquete>.<comando>`. Solo se queda fuera lo que una plataforma exige en un sitio fijo (por ejemplo `api/index.py` en Vercel).
- **`scripts/`:** instaladores `.ps1` y herramientas de desarrollo (comprobaciones, buscar secretos…). **`tests/` es solo para pruebas.**
- **Datos:** la carpeta de datos se llama `datos/`. Los proyectos antiguos que usen otro nombre lo indican en `AGENTS.md`.
- **Pruebas:** cuando un archivo de pruebas pase de unas 500 líneas, divídelo por áreas (`test_reglas.py`, `test_interfaz.py`…).
- **Nada de archivos de reglas de una herramienta concreta** (`.clinerules`, `.cursorrules`…): todo va en `AGENTS.md`, que leen todas.
- **La lógica va separada de la interfaz:** una capa de servicio que usan igual la ventana y la línea de comandos.

## 3. Instalación: lo que tiene que hacer la app

### Apps locales (se ejecutan en mi portátil)
- **`Instalar.bat`** (doble clic) llama a `powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\install.ps1"` y termina con `pause`.
- **`scripts\install.ps1`** trabaja sobre la carpeta del proyecto (`Split-Path $PSScriptRoot -Parent`) y avanza por pasos numerados (`[1/4]`, `[2/4]`…):
  1. Comprueba los requisitos (Python, versión…). Si falta algo, dice qué instalar, con enlace, y para.
  2. Instala las dependencias.
  3. Crea la configuración copiando el ejemplo, **sin sobrescribir nunca la que ya exista**.
  4. Crea accesos directos en el Escritorio y en el menú Inicio.
  5. Abre la app y dice cuál es el siguiente paso.
- **Se puede repetir** sin romper nada. Sirve para reparar, después de actualizar o después de mover la carpeta.
- **`Desinstalar.bat`** quita lo que instaló (tareas programadas, accesos directos) y **nunca borra datos del usuario**.
- **Errores:** siempre un mensaje en español que diga qué hacer, nunca solo una traza de error. Los logs van a `datos/`.
- **Los `.ps1`** deben funcionar en Windows PowerShell 5.1, así que **solo caracteres ASCII** (sin tildes ni eñes).
- **Si usa algo fuera de la app** (tareas programadas, notificaciones, accesos directos), se prueba de verdad y luego se deshace la prueba.

### Apps web o en la nube (Vercel, GitHub Actions, bases de datos…)
- **Cada servicio externo** se explica en su propio paso: dónde crear la cuenta, **en qué botón hacer clic**, qué valor copiar y dónde pegarlo.
- **Tabla de variables y secretos:** nombre, para qué sirve, de dónde sale y dónde se pone.
- **Comprobación final:** una forma de saber que todo funciona ("abre la URL y deberías ver…").
- **Uso local opcional**, también con `.bat` si se puede.

## 4. README: plantilla obligatoria

El README es para **usar la app**, no para entender el código. Secciones, en este orden:

1. **Título y una frase** que diga qué hace, con una captura de la pantalla principal.
2. **Qué hace:** de 3 a 6 puntos, contados como beneficios.
3. **Antes de empezar:** cada requisito con enlace de descarga, versión mínima y **qué casilla marcar** al instalarlo. Las cuentas o servicios necesarios.
4. **Instalación paso a paso:** empieza en "estoy en la página de GitHub del proyecto".
   - Incluye dos caminos: **ZIP** (sin git) y **git**.
   - Indica **dónde guardar la carpeta** y por qué.
   - Explica **qué hacer con los avisos de seguridad de Windows**.
5. **Primeros pasos:** recorrido guiado desde abrir la app hasta tener el primer resultado.
6. **Uso diario:** qué hace cada pantalla o botón, y dónde quedan las cosas.
7. **Configuración:** qué se puede ajustar, dónde y con un ejemplo. Para datos privados: qué dato hace falta, cómo conseguirlo y dónde ponerlo.
8. **Actualizar** a una versión nueva sin perder la configuración ni los datos.
9. **Desinstalar.**
10. **Problemas frecuentes:** tabla "Si pasa esto → haz esto".
11. **Para desarrollar:** pruebas, estructura (enlace a `AGENTS.md`) y `CHANGELOG.md`.

### Cómo redactarlo
- Escribe para alguien que **no programa** y que sigue los pasos uno a uno sin saltarse ninguno.
- Pasos numerados, **una acción por paso**, y después de cada paso, **qué debería verse**.
- **Nombres exactos** de botones, pestañas, archivos y casillas, en negrita, tal como aparecen en pantalla.
- No escribas "simplemente", "obviamente" ni jerga sin explicar.
- **Consola:** solo como alternativa o en "Para desarrollar", nunca como el único camino.
- **Cada paso se comprueba con el código real** antes de escribirlo. No documentes nada que no hayas verificado.
- **Capturas** en `docs/capturas/`, hechas con datos de ejemplo. Nunca con rutas, nombres ni datos reales.

## 5. Datos privados y configuración

- **Configuración:** en el repo va solo `config.example.*` o `.env.example`, comentado. El archivo real está en `.gitignore`.
- **Opciones nuevas:** valor por defecto en el código, para que las configuraciones antiguas sigan funcionando después de actualizar.
- **Nunca en el repo** (ni en pruebas, capturas, logs o mensajes de commit): claves, tokens, contraseñas, emails, rutas personales ni datos personales.
- **Identidad de git:** `Pol <236803046+polreig@users.noreply.github.com>`. Nunca el email del trabajo.

## 6. Código y pruebas

- **Python:** versión 3.11 o superior. Mejor la biblioteca estándar; cada dependencia nueva tiene que estar justificada. Las dependencias opcionales se importan dentro de un `try` y la app funciona sin ellas.
- **Pruebas:** una por cada cambio de lógica, con `unittest`, en `tests/`. Siempre con **carpetas y datos temporales**: nunca se tocan los archivos, cuentas ni datos reales del usuario.
- **Acciones que modifican o borran algo:**
  - vista previa antes de actuar;
  - siempre reversibles (deshacer, papelera o cuarentena);
  - nunca sobrescribir sin preguntar.
- **Interfaces locales:** solo en `127.0.0.1`, con token por sesión, y el servidor valida todo lo que llega del navegador.
- **Interfaz:** cada función nueva tiene que poder usarse desde la interfaz, no solo desde la consola.

## 7. Antes de dar un cambio por terminado

- [ ] Las pruebas pasan.
- [ ] El README está al día con cualquier cambio que el usuario vea o haga. Los pasos de instalación están **revisados como si fuera la primera vez**.
- [ ] Las capturas están actualizadas si cambió la interfaz.
- [ ] `CHANGELOG.md` tiene una entrada nueva (versión, fecha, cambios en lenguaje de usuario) y la versión está subida en el código.
- [ ] `AGENTS.md` está al día si cambiaron la estructura, los comandos o alguna decisión.
- [ ] Las opciones nuevas están en `config.example.*`.
- [ ] No hay datos personales en los cambios.
- [ ] Al terminar, explícame en pocas líneas: **qué ha cambiado, cómo lo pruebo y qué mensaje de commit propones**.

## 8. Plantilla de AGENTS.md (lo propio de cada proyecto)

```
# AGENTS.md — <Nombre>
Sigue ESTANDAR_PROYECTOS.md (obligatorio). Aquí solo va lo propio de este proyecto.

## Qué es            una o dos frases y para quién
## Comandos          instalar, ejecutar, pruebas
## Estructura        qué hace cada archivo o carpeta
## Reglas propias    lo que nunca se debe romper en este proyecto
## Decisiones        por qué se hizo así (para no deshacerlo sin querer)
## Pendiente         lo que falta para cumplir el estándar o ideas acordadas
```

## 9. Proyecto nuevo

1. Crea el repo en `C:\Users\<tu usuario>\Proyectos\<nombre-con-guiones>` con la estructura del punto 2 y copia este archivo tal cual.
2. Escribe `AGENTS.md` con la plantilla del punto 8 y `CLAUDE.md` con sus dos líneas.
3. Escribe el README con la plantilla del punto 4 desde el primer día, aunque esté corto.
4. Configura la identidad de git del punto 5 en el repo antes del primer commit.
5. Empieza `CHANGELOG.md` con la versión `0.1.0`.

## 10. Cambios del estándar

- **v2 (2026-11-10):**
  - Dónde viven los proyectos y cómo se llaman (un nivel, minúsculas y guiones).
  - La raíz, solo para el usuario; los `.ps1` y las herramientas, en `scripts/`; `tests/` solo para pruebas.
  - El código, siempre dentro de un paquete.
  - La carpeta de datos se llama `datos/`.
  - Dividir las pruebas cuando crecen.
  - Sin archivos de reglas de una herramienta concreta.
- **v1 (2026-11-10):** primera versión.
