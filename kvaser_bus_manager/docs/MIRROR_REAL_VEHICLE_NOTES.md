# Mirror reale MLBevo (VAG) — note tecniche e stato software

_Data aggiornamento: 2026-02-11_

Questo documento riassume **tutto quello che abbiamo imparato** sul mirror reale della vettura (Gateway MLBevo) e lo stato del software in questa repo.

## 1) Obiettivo

- Abilitare il **bus mirroring reale** sul Gateway via diagnostica (UDS/DoIP).
- Ricevere i pacchetti mirror su Ethernet (UDP) verso il target (Raspberry / host).
- Parsare i pacchetti per estrarre frame CAN reali (ID + dati) e alimentarli nella pipeline (decoding DBC / comparison engine / UI).
- Produrre anche un artefatto **MF4** riproducibile.

## 2) Abilitazione mirror via UDS

### DID corretto

- **Mirror_mode DID = `0x096F`** (scoperto dai file PDX/ODX)
- DID trovati ma non correlati:
  - `0x2A3C` = Production_process_data_1 (non mirror)
  - `0x2A00` = stato/flag (read-only, non affidabile per dire se il mirror sta trasmettendo)
  - `0x189A` = Mirror_mode_bus_mapping (read)

### Struttura DID 0x096F (21 bytes)

```
[0]  Target_bus (0=not_active, 1=CAN_diag, 2=Ethernet)
[1]  CAN1-8 bitmask
[2]  FR/LIN bitmask
[3..18]  IPv6 destination address (16 bytes)
[19..20] Port (uint16 BE)
```

Valori usati con successo:
- `Target_bus = 2` (Ethernet)
- CAN mask: `0xFF` (tutti)
- IPv6: link-local del ricevitore (es. `fe80::...`)
- Port: usato `30490`

### Sessione diagnostica

- Extended session `0x03` sufficiente.
- Development session `0x4F` accettata, ma non necessaria per il mirror.

### [CRITICO] "Development Messages" Master Switch (DID 0x0902)

E' emerso che su alcune versioni software del gateway, il mirror **non invia dati** se non viene prima abilitato un flag globale di "Development Messages".

- **DID**: `0x0902`
- **Nome**: "Activation and Deactivation of all Development Messages"
- **Operazione**: WriteDataByIdentifier (0x2E)
- **Valore da scrivere**: `0x01` (Enable) o bitmask `0x0F`. Il valore `0x01` (Enable Group 1) è risultato funzionante.
- **Sintomo**: Se questo DID è a `0x00`, il DID mirror `0x096F` accetta la configurazione ma **nessun pacchetto UDP esce dal gateway**.
- **Fix**: Il software ora controlla e forza `0x0902` a `0x01` prima di attivare il mirror.

### [CRITICO] Indirizzamento IP (IPv6 vs IPv4)

Il gateway ha mostrato problemi nell'invio di pacchetti mirror verso indirizzi IPv4 (`192.168.200.1`), probabilmente dovuti a mancata risoluzione ARP o routing incompleto quando non è presente un tester DHCP.

- **Soluzione**: Utilizzare **IPv6 Link-Local** (`fe80::...`).
- Il protocollo DoIP opera nativamente su IPv6.
- Il software ora rileva automaticamente l'indirizzo IPv6 Link-Local dell'interfaccia `eth0` (o configurata) e lo invia nel payload del DID `0x096F`.

## 3) Trasporto: SOME/IP su UDP

I pacchetti mirror **non** sono AUTOSAR Bus Mirroring “nativi”.

Il Gateway invia:
- UDP payload che inizia con **header SOME/IP (16 bytes)**
- Service ID: `0x02FD`
- Method/Event ID: `0xF302`

Conseguenza fondamentale:
- Se si prova a parsare il payload con decoder AUTOSAR/raw, si ottengono **CAN ID spazzatura** (esempio: `0x2FDF302` = service+method visti come arb_id).

## 4) Formato payload interno (VAG proprietario)

Dopo lo stripping del SOME/IP header (16B), rimane un payload “inner”.

Le catture reali mostrano:
- esiste un piccolo header iniziale (spesso 4 bytes, tipo len+flags)
- poi record ripetuti che includono:
  - bus channel (0..7)
  - network type (CAN vs status)
  - CAN ID e DLC
  - data
- presenza di **status blocks** e padding/alignment che possono desincronizzare un parser “a stride fisso”.

Per questo, la strategia robusta è un parser **resync / scan**.

## 5) Parser implementato (stato attuale)

### File
- `kvaser_bus_manager/backend/ethernet_capture.py`

### Scelte progettuali
- Riconoscimento SOME/IP (`0x02FD/0xF302`) **prima di tutto**.
- Se match SOME/IP: **non si fa fallthrough** su AUTOSAR/raw.
- Parser VAG: **scanner con resync** (byte-by-byte) che tenta un layout candidato e valida:
  - bus in `0..7`
  - `net_type == 1` (solo CAN classico; CAN-FD ignorato)
  - `dlc <= 64`
- Se non trova record plausibili: non emette frame.

### Nota importante
Il layout esatto non è ancora “spec ufficiale”: è derivato empiricamente.
Lo scanner è scelto proprio per essere tollerante a varianti/padding.

## 6) Test aggiunti

- Unit test sintetici per mirror VAG in `kvaser_bus_manager/tests/test_mirror_parsing.py`
- **Golden test** basato su una cattura reale:
  - `kvaser_bus_manager/tests/data/vag_mirror_single_payload.bin`
  - Il test verifica che dal payload reale si estraggano frame e ID noti (es. `0x3D5`, `0x86`, `0x108`, `0xFD`).

## 7) Artefatto MF4 (reale)

È stato creato un MF4 “offline” a partire da un PCAP con un pacchetto mirror reale.

### Script
- `kvaser_bus_manager/scripts/generate_mf4_from_pcap.py`

### Output (già generato)
- `kvaser_bus_manager/logs/mirror_trace_from_pcap.mf4`

Contiene segnali minimi:
- `Mirror_CAN_ID`
- `Mirror_CAN_Bus`
- `Mirror_CAN_DLC`
- `Mirror_CAN_Data` (bytes fissi S64)

Questo MF4 è un artefatto riproducibile e valido (leggibile con `asammdf`).

## 8) UI: cosa dovrebbe mostrare

Aspettative quando il mirror è attivo:
- Sorgenti/canali coerenti (CAN1..CAN8 mappati su channel_id 100..107)
- CAN ID plausibili (match ARXML/DBC)
- Nessun ID spurio derivato da SOME/IP (es. `0x2FDF302`)

Se la UI mostra ancora ID spazzatura, la prima cosa da controllare è:
- che il backend in esecuzione includa l’ultima versione di `ethernet_capture.py`
- che non esistano processi vecchi rimasti attivi

## 9) Come verificare rapidamente end-to-end

1) Mirror attivo (vettura): si vedono UDP packet su porta configurata.
2) Backend:
   - log di alcuni frame mirror con ID plausibili
   - endpoint `/api/eth/status` e `/api/sources` coerenti
3) Decoding:
   - DBC loader decodifica ID noti (es. `0x0FD`, `0x0A8`, `0x0116`, ...)

## 10) Limitazioni / next steps

- Raffinare la logica di scoring per ridurre falsi positivi (es. record con `arb_id=0` o `dlc=0`).
- Aggiungere una seconda variante di layout (se emergono altri pattern su catture più lunghe).
- Collegare il logging MF4 anche alla pipeline live (oltre allo script offline), includendo eventuali timestamp migliori.
