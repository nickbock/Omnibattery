# Zinvolt

Omnibattery controla una batería Zinvolt a través de la integración Zinvolt incluida en Home Assistant. Esa integración gestiona el inicio de sesión en la nube; Omnibattery solo lee y escribe sus entidades.

## ¿La necesito?

**Úsala si** tu Zinvolt ya funciona en la integración Zinvolt de Home Assistant y usas Home Assistant 2026.11 o posterior. **Espera un control lento:** Zinvolt no tiene interfaz local, así que cada orden pasa por la nube de Zinvolt, y Home Assistant lee la batería cada 30 s.

## Antes de empezar

- Añade la integración Zinvolt en Home Assistant y comprueba que las entidades **Mode**, **Maximum output** y **Maximum charge power** de la batería existen y están habilitadas.
- Desactiva automatizaciones, programaciones o apps que cambien el modo o los límites de potencia de Zinvolt. Omnibattery toma el control de ambos.
- Ten a mano la capacidad nominal de la batería; la integración no la indica.

## Cómo añadirla

1. En el asistente de Omnibattery, elige **Zinvolt (Home Assistant integration)**.
2. Introduce un **Nombre** descriptivo y elige el **Dispositivo Zinvolt** de la batería. Elige la batería, no una de sus unidades de pack.
3. Introduce la capacidad nominal y elige los límites comunes de potencia y estado de carga.

## Qué verás

Omnibattery envía un único objetivo de potencia neta y lo traduce al modo y los límites de Zinvolt:

| Objetivo | Modo Zinvolt | Límite escrito |
|---|---|---|
| Carga | Fast charge | Maximum charge power = objetivo |
| Descarga | Fast discharge | Maximum output = objetivo |
| Reposo | Fast charge | Maximum charge power = 0 ("Inactive" en la app de Zinvolt) |

La batería se queda en reposo en ese estado cuando Omnibattery se detiene. El estado de carga (SOC) y la potencia de la batería aparecen como entidades.

## Si no funciona

| Síntoma | Causa probable | Qué comprobar |
|---|---|---|
| El asistente indica entidades que faltan | Se eligió una subunidad, hay una entidad deshabilitada o Home Assistant es anterior a 2026.11 | Elige el dispositivo de la batería, habilita las entidades indicadas y actualiza Home Assistant |
| Las órdenes tardan un minuto en verse | Tiempo muerto de la nube más la rampa de la batería | Espera hasta dos minutos en una inversión completa |
| La batería cambia de modo sola | Otra automatización o la app de Zinvolt cambia el modo | Desactiva esa automatización |
| La batería pasa a no disponible | La nube de Zinvolt o la batería están desconectadas | Revisa la integración Zinvolt |

??? "Detalles avanzados"
    Medido en una Zinvolt con 2.000 W de salida: la batería empieza a moverse 8–13 s después de una orden y luego sube o baja unos 25 W/s, así que pasar de 2.000 W de carga a descarga tarda unos 90 s. Por eso el driver escribe como mucho cada 30 s e ignora cambios menores de 100 W, y sus capacidades de tiempo son conservadoras.

    Una batería que solo funciona por la nube no se puede detener si se pierde la conexión: si Home Assistant o la nube de Zinvolt caen, la batería mantiene su último modo hasta que la paran sus propios niveles de carga.

    El rango es de 2.000 W en ambos sentidos; una unidad con la salida aún bloqueada limita la descarga al límite de la entidad **Maximum output** (800 W). Los límites de SOC se aplican por software.

    Para controles comunes y límites del sistema, consulta [Elegir la conexión de tu batería](index.md).
