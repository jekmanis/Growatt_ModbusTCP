# WIT Mode Register Matrix

Pilns pārskats par to, kuri reģistri tiek iestatīti katrā `set_wit_mode` režīmā.

**Apzīmējumi:**
- **Vērtība** = reģistrs tiek aktīvi ierakstīts ar šo vērtību
- `power%` = `power_percent` parametra vērtība (noklusējums 100)
- `65536-p` = unsigned 16-bit kodējums negatīvai jaudai (discharge)
- `soc%*` = tiek rakstīts tikai ja parametrs eksplicīti norādīts servisa izsaukumā

**Princips: katrs režīms iestata VISUS kritiskos reģistrus. Nav mantojuma starp režīmiem.**

---

## Galvenā matrica

| Reģistrs | Apraksts | Grid Charge | Discharge to Load | Discharge to Grid | Max Export | Preserve SOC | Passthrough |
|---|---|---|---|---|---|---|---|
| **30100** | VPP Control Authority | **1** | **1** | **1** | **1** | **1** | **1** |
| **30476** | Priority Mode | **1** (Bat First) | **1** (Bat First) | **1** (Bat First) | **1** (Bat First) | **0** (Load First) | **0** (Load First) |
| **30410** | AC Charge Enable | **1** (PV priority) | **0** (disabled) | **0** (disabled) | **0** (disabled) | **0** (disabled) | **0** (disabled) |
| **30404** | Charge Cutoff SOC | soc%* | soc%* | soc%* | soc%* | soc%* | soc%* |
| **30405** | Discharge Cutoff SOC | soc%* | soc%* | soc%* | soc%* | soc%* | soc%* |
| **30200** | Export Limit Enable | **0** (off) | **1** (on) | **0** (off) | **0** (off) | **0** (off) | **0** (off) |
| **30201** | Export Limit Rate | -- | **0** (zero export) | -- | -- | -- | -- |
| **30411** | TOU Period Count | **0** (clear) | **0** (clear) | **0** (clear) | **0** (clear) | **0** (clear) | **0** (clear) |
| **30408** | Remote Power Duration | **duration** min | **duration** min | **duration** min | **duration** min | **0** (clear) | **0** (clear) |
| **30409** | Remote Power % | **+power%** | **65536-power%** | **65536-power%** | **65436** (=-100%) | **0** (clear) | **0** (clear) |
| **30407** | Remote Power Enable | **1** (on) | **1** (on) | **1** (on) | **1** (on) | **0** (off) | **0** (off) |

### Kopsavilkums: reģistri, kas eksplicīti iestatīti katrā režīmā

| Reģistrs | Grid Charge | Disch→Load | Disch→Grid | Max Export | Preserve SOC | Passthrough |
|---|:---:|:---:|:---:|:---:|:---:|:---:|
| 30100 | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| 30476 | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| 30410 | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| 30200 | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| 30201 | -- | ✓ | -- | -- | -- | -- |
| 30411 | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| 30408 | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| 30409 | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| 30407 | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |

`--` = 30201 netiek rakstīts kad 30200=0 (limiteris izslēgts, rate vērtība nerelevanta).

---

## Detalizēts apraksts pa reģistriem

### 30100 — VPP Control Authority
Vienmēr **1** visos režīmos. Nodrošina VPP kontroles tiesības.

### 30476 — Priority Mode (Battery First / Load First)

| Režīms | Vērtība | Piezīme |
|---|---|---|
| **Grid Charge** | **1** (Battery First) | **OBLIGĀTS!** Ar 30476=0 vai 30476=2 lādēšana dod 0W |
| **Discharge to Load** | **1** (Battery First) | PV surplus lādē akumulatoru; ar 30476=0 tas nenotiek |
| **Discharge to Grid** | **1** (Battery First) | PV surplus lādē akumulatoru izlādes laikā |
| **Max Export** | **1** (Battery First) | Konsistenti ar citiem 30407=1 režīmiem |
| **Preserve SOC** | **0** (Load First) | Drošs — novērš akumulatora izlādi uz tīklu |
| **Passthrough** | **0** (Load First) | Drošs — inverters seko normālai Load First uzvedībai |

**Kāpēc tieši šī dalījums?**

Reģistrs 30476 ietekmē invertora uzvedību **gan ar, gan bez** remote control (30407):

| 30476 | Kad 30407=0 | Kad 30407=1 |
|---|---|---|
| 0 (Load First) | PV → load → battery → export. **Drošs.** | **Grid charge = 0W!** PV surplus neiet uz akumulatoru. |
| 1 (Battery First) | Prioritāte akum. lādēšanai no visiem avotiem | **Grid charge strādā!** PV surplus lādē akumulatoru. |
| 2 (Grid First) | **Akumulators izlādējas uz tīklu 6kW!** | Netestēts / nedrošs |

Atklāts 2026-03-30: grid_charge ar 30476=0 → 0W; ar 30476=1 → 3kW+. Vērtība 1 nekad netika testēta atsevišķi — tā bija trūkstošā detaļa.

### 30410 — AC Charge Enable
| Režīms | Vērtība | Piezīme |
|---|---|---|
| **Grid Charge** | **1** (PV priority) | AC priority (2) noraidīts V1.39 firmware |
| Discharge to Load | **0** (disabled) | Nav nepieciešama tīkla lādēšana |
| Discharge to Grid | **0** (disabled) | Nav nepieciešama tīkla lādēšana |
| Max Export | **0** (disabled) | Nav nepieciešama tīkla lādēšana |
| Preserve SOC | **0** (disabled) | Nav nepieciešama tīkla lādēšana |
| Passthrough | **0** (disabled) | Defensīvi notīra, lai nepaliktu stale vērtība |

Ja `ac_charge_mode` parametrs norādīts servisa izsaukumā, tas pārraksta režīma noklusējumu.

### 30404 / 30405 — SOC Cutoff Limits
Tiek rakstīts **tikai tad**, ja `charge_cutoff_soc` vai `discharge_cutoff_soc` parametrs eksplicīti norādīts.

| Parametrs | Tipiski ar | Preset noklusējums |
|---|---|---|
| charge_cutoff_soc (30404) | Grid Charge | 100 |
| discharge_cutoff_soc (30405) | Discharge režīmi | 10 |

**Piezīme:** Šie ir vienīgie reģistri, kas var palikt mantojumā — bet tikai tad, ja lietotājs tos nenorada. Praktiski preset vienmēr norāda SOC vērtības.

### 30200 — Export Limit Enable
| Režīms | Vērtība | Piezīme |
|---|---|---|
| Grid Charge | **0** | Limiteris izslēgts — novecojis 30200=1 bloķē lādēšanu |
| **Discharge to Load** | **1** | Limiteris ieslēgts — zero export |
| Discharge to Grid | **0** | Limiteris izslēgts — eksports atļauts |
| Max Export | **0** | Limiteris izslēgts — maksimālais eksports |
| Preserve SOC | **0** | Limiteris izslēgts — PV surplus var eksportēt |
| Passthrough | **0** | Limiteris izslēgts — notīra novecojušas vērtības |

Vienmēr tiek eksplicīti iestatīts katrā režīmā.

### 30201 — Export Limit Rate
| Režīms | Vērtība | Piezīme |
|---|---|---|
| **Discharge to Load** | **0** | Zero export — jauda nesūtāma uz tīklu |
| Visi pārējie | -- | Nav nepieciešams (30200=0 izslēdz limiteri) |

Tiek rakstīts tikai kopā ar 30200=1. Ja `export_rate` parametrs norādīts, tiek rakstīts ar to vērtību.

### 30411 — TOU Period Count
Vienmēr **0** visos režīmos. Notīra TOU periodu paliekas.

### 30408 — Remote Power Duration
| Režīms | Vērtība | Piezīme |
|---|---|---|
| Grid Charge | **duration_minutes** | Noklusējums: 60 (preset: 120) |
| Discharge to Load | **duration_minutes** | Noklusējums: 60 (preset: 120) |
| Discharge to Grid | **duration_minutes** | Noklusējums: 60 (preset: 120) |
| Max Export | **duration_minutes** | Noklusējums: 60 (preset: 120) |
| Preserve SOC | **0** (clear) | Defensīvi nodzēš — ja 30407 tiek atkārtoti ieslēgts ārēji, taimeris nesāksies |
| Passthrough | **0** (clear) | Defensīvi nodzēš stale vērtību |

### 30409 — Remote Power Percent
| Režīms | Vērtība | Nozīme |
|---|---|---|
| **Grid Charge** | **power%** (1-100) | Pozitīvs = lādēšana |
| **Discharge to Load** | **65536 - power%** | Negatīvs (unsigned) = izlāde |
| **Discharge to Grid** | **65536 - power%** | Negatīvs (unsigned) = izlāde |
| **Max Export** | **65436** (= 65536-100) | Vienmēr 100% izlāde |
| **Preserve SOC** | **0** (clear) | Defensīvi nodzēš — neļauj stale charge/discharge komandai palikt hardware |
| **Passthrough** | **0** (clear) | Defensīvi nodzēš stale vērtību |

### 30407 — Remote Power Enable (VIENMĒR PĒDĒJAIS)
| Režīms | Vērtība | Piezīme |
|---|---|---|
| Grid Charge | **1** | Remote control aktīvs — inverters seko 30409 komandai |
| Discharge to Load | **1** | Remote control aktīvs |
| Discharge to Grid | **1** | Remote control aktīvs |
| Max Export | **1** | Remote control aktīvs |
| **Preserve SOC** | **0** | Remote control izslēgts — ļauj PV eksportēt |
| **Passthrough** | **0** | Remote control izslēgts — inverters seko 30476 |

---

## Rakstīšanas secība

```
 1.  30100 = 1              (VPP control authority — vienmēr)
 2.  30476 = 0 vai 1        (priority mode — VIENMĒR, KATRAM režīmam)
 3.  30410 = 0 vai 1        (AC charge mode)
 4.  30404 = soc%           (charge cutoff, ja norādīts)
 5.  30405 = soc%           (discharge cutoff, ja norādīts)
 6.  30200 = 0/1            (export limit enable — vienmēr)
 7.  30201 = rate%           (export limit rate, ja 30200=1)
 8.  30411 = 0              (TOU periodu notīrīšana — vienmēr)
 9.  30408 = minutes        (override ilgums)
10.  30409 = power%          (jaudas komanda)
11.  30407 = 0/1            (remote control — VIENMĒR PĒDĒJAIS)
```

---

## Mantojuma analīze

### Reģistri bez mantojuma riska (iestatīti VISOS režīmos):
- **30100** — vienmēr 1
- **30476** — vienmēr eksplicīti (1 vai 0)
- **30410** — vienmēr eksplicīti (0 vai 1)
- **30200** — vienmēr eksplicīti (0 vai 1)
- **30411** — vienmēr 0
- **30408** — vienmēr eksplicīti (duration vai 0)
- **30409** — vienmēr eksplicīti (power vai 0)
- **30407** — vienmēr eksplicīti (0 vai 1)

### Reģistri ar minimālu mantojuma risku:
- **30201** — rakstīts tikai ar 30200=1 (kad 30200=0, vērtība ir nerelevanta)

### Reģistri, kas var palikt mantojumā:
- **30404** (charge cutoff SOC) — mainīts tikai ja parametrs norādīts. Praktiski preset vienmēr norāda.
- **30405** (discharge cutoff SOC) — tāpat kā 30404.

### Iepriekš atrastie mantojuma bugi (tagad izlaboti):

| # | Scenārijs | Bugs | Cēlonis | Labojums |
|---|---|---|---|---|
| 1 | `discharge_to_load` → `grid_charge` | Lādēšana bloķēta | 30200=1 (zero export) palika no discharge | Tagad grid_charge iestata 30200=0 |
| 2 | Jebkurš → `preserve_soc` | Akumulators izlādējas 6kW | 30476=2 (Grid First) palika | Tagad preserve_soc iestata 30476=0 |
| 3 | `preserve_soc` → `grid_charge` | Lādēšana = 0W | 30476=0 (Load First) palika | **Tagad grid_charge iestata 30476=1** |
| 4 | `preserve_soc` → `discharge_to_load` | PV surplus nelādē akumulatoru | 30476=0 palika | **Tagad discharge iestata 30476=1** |
| 5 | `grid_charge` → `discharge_to_load` | AC charge joprojām aktīvs | 30410=1 palika | Tagad discharge iestata 30410=0 |

---

## Preset noklusējumi (no select entity)

Kad režīms tiek izvēlēts caur **Mode Preset** dropdown:

| Parametrs | Grid Charge | Discharge to Load | Discharge to Grid | Max Export | Preserve SOC | Passthrough |
|---|---|---|---|---|---|---|
| power_percent | 100 | 100 | 100 | *(nav)* | *(nav)* | *(nav)* |
| duration_minutes | 120 | 120 | 120 | 120 | 120 | *(nav)* |
| ac_charge_mode | pv_priority | *(nav)* | *(nav)* | *(nav)* | *(nav)* | *(nav)* |
| charge_cutoff_soc | 100 | *(nav)* | *(nav)* | *(nav)* | *(nav)* | *(nav)* |
| discharge_cutoff_soc | *(nav)* | 10 | 10 | 10 | *(nav)* | *(nav)* |

---

## Grid Charge priekšnosacījumi (pilns saraksts)

Lai grid charging strādātu uz WIT V1.39, **visi** šie reģistri ir nepieciešami:

```
30100 = 1        (VPP authority ON)
30476 = 1        (Battery First — OBLIGĀTS! 0 un 2 dod 0W)
30410 = 1        (AC charge = PV priority)
30200 = 0        (Export limit OFF — novecojis zero-export bloķē lādēšanu)
30404 = 100      (Charge cutoff SOC — cik pilnu lādēt)
30411 = 0        (TOU clear — TOU periods nedrīkst traucēt)
30408 = duration (Ilgums minūtēs)
30409 = +power%  (Pozitīvs = lādēšana)
30407 = 1        (Remote enable — PĒDĒJAIS, sāk taimeri)
```

Ja kaut viens no 30476=1, 30410=1, 30200=0 trūkst → lādēšana nenotiek (0W).

---

*Dokuments atjaunots: 2026-03-30*
*v3: 30476 tagad iestatīts katram režīmam. Grid charge prasa 30476=1.*
*Bāzēts uz kodu: `diagnostic.py` set_wit_mode, `select.py` WIT_MODE_PRESETS*
