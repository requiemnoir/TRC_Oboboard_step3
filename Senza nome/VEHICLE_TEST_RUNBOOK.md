# Vehicle Test Runbook (Kanvaser)

Data: 2026-01-25

## Scopo
Preparare e tracciare in modo riproducibile il test in vettura, e documentare le correzioni fatte per:
- "lacune" nella discovery ECU via DoIP
- crash/interruzione quando si interroga la ECU 0x0002 (cambio)

## Stato attuale (cosa è stato corretto)
### 1) Fix DoIP: ACK non più interpretati come risposta UDS
File: kvaser_bus_manager/backend/vag_scanner.py

Nella classe `DoIPGatewayScanner`, la funzione `_uds_transact()` prima accettava anche payload type DoIP 0x8002/0x8003 (ACK/NACK) e rischiava di trattarli come risposta UDS.

Ora:
- vengono ignorati 0x8002/0x8003
- viene considerato valido solo 0x8001 con payload UDS non vuoto

Impatto atteso:
- discovery più completa (meno ECU “saltate”)
- transazioni UDS più stabili

### 2) Fix sicurezza: arricchimento DTC (snapshot/ext-data) reso opt-in
File: kvaser_bus_manager/backend/vag_scanner.py

La funzione `_enrich_dtcs_with_context()` genera molte richieste per DTC (0x19 0x04 e 0x19 0x06). Alcune ECU (tipicamente il cambio) possono essere sensibili a questo traffico.

Ora:
- enrichment disabilitato di default
- per default viene anche esclusa l’ECU logica 0x0002 (cambio)

## Variabili d’ambiente utili per il test in vettura
### Range discovery DoIP
Default: 0x0001..0x00FF

Per estendere:
- `DOIP_DISCOVERY_START=0x0001`
- `DOIP_DISCOVERY_END=0x0FFF`

Suggerimento: aumentare gradualmente (es. 0x01FF, poi 0x03FF, ecc.) per evitare scansioni troppo lente.

### Enrichment DTC
- Abilita enrichment: `DOIP_ENRICH_DTC_CONTEXT=1`
- Lista ECU da escludere (CSV, supporta hex con 0x): `DOIP_ENRICH_SKIP_ADDRS=0x0002,0x1234`

Default attuale:
- enrichment OFF
- skip include 0x0002

## Setup ambiente Python (riproducibile)
Nel repo è stata creata una virtualenv locale:
- `.venv/`

Installazione effettuata:
- `python3 -m venv .venv`
- `. .venv/bin/activate`
- `python -m pip install -U pip wheel setuptools`
- `python -m pip install -r kvaser_bus_manager/requirements.txt`

Note:
- `pytest` è ora disponibile dentro la venv.

## Test rapidi (prima della vettura)
Da root repo:
- `. .venv/bin/activate`
- `python -m py_compile kvaser_bus_manager/backend/vag_scanner.py`
- `pytest -q`

## Test in vettura (procedura consigliata)
1) Accensione quadro/ignition ON (meglio tensione stabile)
2) Avviare il servizio/app come da install attuale
3) Eseguire DoIP scan report (UI o API)
4) Se ci sono ancora ECU mancanti:
   - aumentare `DOIP_DISCOVERY_END` gradualmente
5) Se ECU 0x0002 crea instabilità:
   - tenere enrichment OFF (default)
   - assicurarsi che 0x0002 sia in `DOIP_ENRICH_SKIP_ADDRS`

## Nuovo: pulsante UI “Test Sistema — Self Test (CAN/DoIP)”
Nella UI (modalità ScanTools) è stato aggiunto un pulsante che avvia `action=self_test`.

Cosa fa (non invasivo):
- stampa info base (python/version)
- controlla stato CAN (driver mock/simulazione e canali attivi)
- se hai selezionato un CAN channel attivo: prova un handshake OBD sicuro (Mode 01 PID 00)
- se la rete DoIP è raggiungibile: discovery gateway (se abilitata), TCP connect, routing activation, e 2–3 probe TesterPresent su indirizzi “safe”

Sicurezza:
- per default NON interroga 0x0002 (cambio) durante il self-test

Env opzionali self-test:
- `DOIP_SELFTEST_PROBE_ADDRS=0x0001,0x0003,0x0007` (lista probe)
- `DOIP_SELFTEST_ALLOW_0002=1` (abilita probe 0x0002: sconsigliato)

## Traccia modifiche ai test
Per far funzionare i test in modo consistente, sono stati aggiunti:
- `kvaser_bus_manager/__init__.py`
- `kvaser_bus_manager/backend/__init__.py`
- `pytest.ini` (pythonpath=.)

## Cosa controllare se il problema persiste
- Log DoIP: confermare che arrivano 0x8001 con payload UDS reali
- Se discovery ancora incompleta: valutare che alcune ECU non rispondano a 0x3E 00 o richiedano sessione
- Se il veicolo “crasha” ancora su 0x0002: ridurre ulteriormente traffico (time-out più bassi, rate limit, o skip completo delle query DTC su 0x0002)
