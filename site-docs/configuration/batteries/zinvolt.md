# Zinvolt

Omnibattery controls a Zinvolt battery through the Zinvolt integration that ships with Home Assistant. That integration owns the cloud login; Omnibattery only reads and writes its entities.

## Do I need it?

**Use it if** your Zinvolt already works in Home Assistant's Zinvolt integration and you run Home Assistant 2026.11 or newer. **Expect slow control:** Zinvolt has no local interface, so every command travels through the Zinvolt cloud, and Home Assistant reads the battery every 30 s.

## Before you start

- Add the Zinvolt integration in Home Assistant and check that the battery's **Mode**, **Maximum output** and **Maximum charge power** entities exist and are enabled.
- Turn off automations, schedules or apps that change the Zinvolt mode or power limits. Omnibattery takes over both.
- Know the battery's nominal capacity; the integration does not report it.

## How to add it

1. In the Omnibattery setup flow, choose **Zinvolt (Home Assistant integration)**.
2. Enter a descriptive **Name** and pick the battery's **Zinvolt device**. Pick the battery itself, not one of its battery pack units.
3. Enter nominal capacity and choose the common power and state-of-charge limits.

## What you will see

Omnibattery sends one net power target and translates it into the Zinvolt mode and limits:

| Target | Zinvolt mode | Limit written |
|---|---|---|
| Charge | Fast charge | Maximum charge power = target |
| Discharge | Fast discharge | Maximum output = target |
| Idle | Fast charge | Maximum charge power = 0 (shown as "Inactive" in the Zinvolt app) |

The battery stays idle in that state after Omnibattery stops. Battery state of charge (SOC) and power appear as entities.

## If it does not work

| Symptom | Likely cause | What to check |
|---|---|---|
| The wizard reports missing entities | A sub-unit was picked, an entity is disabled, or Home Assistant is older than 2026.11 | Pick the battery device, enable the listed entities and update Home Assistant |
| Commands take a minute to show | Cloud dead time plus the battery's ramp | Allow up to two minutes for a full reversal |
| The battery keeps switching mode by itself | Another automation or the Zinvolt app is changing the mode | Turn that automation off |
| The battery goes unavailable | The Zinvolt cloud or the battery is offline | Check the Zinvolt integration |

??? "Advanced details"
    Measured on a Zinvolt with 2,000 W output: the battery starts moving 8–13 s after a command and then ramps at about 25 W/s, so a reversal from 2,000 W charge to discharge takes about 90 s. The driver therefore writes at most every 30 s and ignores changes below 100 W, and its timing capabilities are conservative.

    A cloud-only battery cannot be stopped when the connection is lost: if Home Assistant or the Zinvolt cloud goes down, the battery keeps its last mode until its own charge levels stop it.

    The envelope is 2,000 W in both directions; a unit whose output is still locked caps discharge at the **Maximum output** entity's limit (800 W). SOC limits are enforced in software.

    For shared runtime controls and system limits, see [Choose your battery connection](index.md).
