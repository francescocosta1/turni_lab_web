import calendar
import random
from collections import defaultdict
from datetime import date, timedelta

from django.db import transaction

from .models import Dipendente, AssegnazioneTurno, CalendarioMensile, Assenza


MAX_TURNI_CONSECUTIVI = 5
MAX_ITER_BILANCIAMENTO_WEEKEND = 200
MAX_ITER_BILANCIAMENTO_CARICO = 200


def fabbisogno_giornaliero(data_corrente):
    """
    Caso pseudo-reale:
    - lun-ven: M 7, P 5, N 1
    - sabato:  M 5, P 4, N 1
    - domenica: M 4, P 3, N 1
    """
    giorno_settimana = data_corrente.weekday()

    if giorno_settimana <= 4:
        return {"M": 7, "P": 5, "N": 1}
    elif giorno_settimana == 5:
        return {"M": 5, "P": 4, "N": 1}
    else:
        return {"M": 4, "P": 3, "N": 1}


def target_mensile(dipendente):
    if dipendente.tipo_contratto == "full_time":
        return 22
    return 18


def e_turno_lavorativo(turno):
    return turno in ["M", "P", "N"]


def e_weekend(data_corrente):
    return data_corrente.weekday() >= 5


def tipo_assenza_to_sigla(tipo):
    mapping = {
        "ferie": "F",
        "assenza": "A",
        "malattia": "L",
        "permesso": "X",
    }
    return mapping.get(tipo, "A")


def conta_consecutivi_con_assegnazione(mappa, dipendente_id, data_corrente, nuovo_turno):
    if not e_turno_lavorativo(nuovo_turno):
        return 0

    consecutivi = 1

    giorno = data_corrente - timedelta(days=1)
    while True:
        turno = mappa.get(dipendente_id, {}).get(giorno)
        valore = turno.turno if turno else None
        if e_turno_lavorativo(valore):
            consecutivi += 1
            giorno -= timedelta(days=1)
        else:
            break

    giorno = data_corrente + timedelta(days=1)
    while True:
        turno = mappa.get(dipendente_id, {}).get(giorno)
        valore = turno.turno if turno else None
        if e_turno_lavorativo(valore):
            consecutivi += 1
            giorno += timedelta(days=1)
        else:
            break

    return consecutivi


def puo_lavorare_in_data(mappa, dipendente_id, data_corrente, turno):
    if turno not in ["M", "P", "N"]:
        return False

    assegnazione_corrente = mappa.get(dipendente_id, {}).get(data_corrente)
    if not assegnazione_corrente:
        return False

    if assegnazione_corrente.turno != "R":
        return False

    giorno_precedente = data_corrente - timedelta(days=1)
    ass_precedente = mappa.get(dipendente_id, {}).get(giorno_precedente)
    if ass_precedente and ass_precedente.turno == "N":
        return False

    if turno == "N":
        giorno_successivo = data_corrente + timedelta(days=1)
        ass_successiva = mappa.get(dipendente_id, {}).get(giorno_successivo)
        if ass_successiva and ass_successiva.turno != "R":
            return False

    consecutivi = conta_consecutivi_con_assegnazione(mappa, dipendente_id, data_corrente, turno)
    if consecutivi > MAX_TURNI_CONSECUTIVI:
        return False

    return True


def weekend_count_per_ids(mappa, ids_gruppo):
    conteggi = {}
    for dip_id in ids_gruppo:
        conteggi[dip_id] = sum(
            1 for a in mappa[dip_id].values()
            if e_weekend(a.data) and e_turno_lavorativo(a.turno)
        )
    return conteggi


def totale_turni_per_ids(mappa, ids_gruppo):
    conteggi = {}
    for dip_id in ids_gruppo:
        conteggi[dip_id] = sum(
            1 for a in mappa[dip_id].values()
            if e_turno_lavorativo(a.turno)
        )
    return conteggi


def conta_statistiche_dipendente(mappa, dip_id):
    totale = 0
    notti = 0
    weekend = 0

    for a in mappa[dip_id].values():
        if e_turno_lavorativo(a.turno):
            totale += 1
            if a.turno == "N":
                notti += 1
            if e_weekend(a.data):
                weekend += 1

    return totale, notti, weekend


def scegli_sostituto_locale(mappa, dipendenti, assenti_per_data, dipendente_assente_id, data_corrente, turno_da_coprire):
    candidati = []

    for d in dipendenti:
        if d.id == dipendente_assente_id:
            continue

        if d.id in assenti_per_data.get(data_corrente, set()):
            continue

        if turno_da_coprire == "N" and d.tipo_contratto != "full_time":
            continue

        if not puo_lavorare_in_data(mappa, d.id, data_corrente, turno_da_coprire):
            continue

        totale, notti, weekend = conta_statistiche_dipendente(mappa, d.id)

        if turno_da_coprire == "N":
            score = (notti, totale, weekend, random.random())
        else:
            score = (totale, weekend, notti, random.random())

        candidati.append((score, d.id))

    if not candidati:
        return None

    candidati.sort(key=lambda x: x[0])
    dip_id = candidati[0][1]
    return mappa[dip_id][data_corrente]


def puo_cambiare_turno_stesso_giorno(mappa, dipendente_id, data_corrente, nuovo_turno):
    """
    Controllo per cambiare turno nello stesso giorno.
    Qui il dipendente lavora già quel giorno, quindi non stiamo aggiungendo
    un giorno lavorativo in più, ma solo cambiando il tipo di turno.
    """
    if nuovo_turno not in ["M", "P", "N"]:
        return False

    ass_corrente = mappa.get(dipendente_id, {}).get(data_corrente)
    if not ass_corrente:
        return False

    turno_attuale = ass_corrente.turno
    if turno_attuale not in ["M", "P", "N"]:
        return False

    # Notte: solo full-time
    if nuovo_turno == "N" and ass_corrente.dipendente.tipo_contratto != "full_time":
        return False

    # Se il giorno prima ha fatto notte, oggi non dovrebbe lavorare
    # ma se è già assegnato a un turno lavorativo, assumiamo che il calendario
    # corrente sia già valido e quindi consentiamo il cambio tra turni lavorativi.
    # Non aggiungiamo ulteriori vincoli qui.

    # Se lo sposto a notte, il giorno successivo deve essere riposo
    if nuovo_turno == "N":
        giorno_successivo = data_corrente + timedelta(days=1)
        ass_successiva = mappa.get(dipendente_id, {}).get(giorno_successivo)
        if ass_successiva and ass_successiva.turno != "R":
            return False

    return True


def trova_catena_copertura(
    mappa,
    dipendenti,
    assenti_per_data,
    data_corrente,
    turno_da_coprire,
    dipendente_escluso_id,
    profondita_max=3,
):
    """
    Cerca una catena di copertura nello stesso giorno.

    Restituisce una lista di operazioni del tipo:
    [
        (assegnazione_obj, "NUOVO_TURNO"),
        ...
    ]

    Significato:
    - assegna quel nuovo turno a quell'assegnazione
    """
    visitati = set()

    def dfs(turno_scoperto, esclusi_ids, profondita):
        stato = (turno_scoperto, tuple(sorted(esclusi_ids)), profondita)
        if stato in visitati:
            return None
        visitati.add(stato)

        if profondita > profondita_max:
            return None

        # 1. Prova copertura diretta con qualcuno a riposo
        candidati_riposo = []
        for d in dipendenti:
            if d.id in esclusi_ids:
                continue
            if d.id in assenti_per_data.get(data_corrente, set()):
                continue

            ass = mappa.get(d.id, {}).get(data_corrente)
            if not ass:
                continue

            if ass.turno != "R":
                continue

            if turno_scoperto == "N" and d.tipo_contratto != "full_time":
                continue

            if not puo_lavorare_in_data(mappa, d.id, data_corrente, turno_scoperto):
                continue

            totale, notti, weekend = conta_statistiche_dipendente(mappa, d.id)
            score = (totale, notti, weekend, random.random())
            candidati_riposo.append((score, ass))

        candidati_riposo.sort(key=lambda x: x[0])

        if candidati_riposo:
            ass_r = candidati_riposo[0][1]
            return [(ass_r, turno_scoperto)]

        # 2. Prova a spostare qualcuno che già lavora quel giorno
        candidati_lavorativi = []
        for d in dipendenti:
            if d.id in esclusi_ids:
                continue
            if d.id in assenti_per_data.get(data_corrente, set()):
                continue

            ass = mappa.get(d.id, {}).get(data_corrente)
            if not ass:
                continue

            if ass.turno not in ["M", "P", "N"]:
                continue

            if ass.turno == turno_scoperto:
                continue

            if not puo_cambiare_turno_stesso_giorno(mappa, d.id, data_corrente, turno_scoperto):
                continue

            totale, notti, weekend = conta_statistiche_dipendente(mappa, d.id)
            score = (totale, notti, weekend, random.random())
            candidati_lavorativi.append((score, ass))

        candidati_lavorativi.sort(key=lambda x: x[0])

        for _, ass_lav in candidati_lavorativi:
            turno_liberato = ass_lav.turno
            nuovo_esclusi = set(esclusi_ids)
            nuovo_esclusi.add(ass_lav.dipendente_id)

            sotto_soluzione = dfs(turno_liberato, nuovo_esclusi, profondita + 1)
            if sotto_soluzione is not None:
                return [(ass_lav, turno_scoperto)] + sotto_soluzione

        return None

    return dfs(turno_da_coprire, {dipendente_escluso_id}, 0)

def scegli_scambio_locale(mappa, dipendenti, assenti_per_data, dipendente_assente_id, data_corrente, turno_da_coprire):
    """
    V2: prova uno scambio locale nello stesso giorno.

    Schema:
    - il dipendente assente lascia scoperto turno_da_coprire
    - cerco un collega A che quel giorno lavora un turno M/P
    - cerco un collega B che quel giorno è a riposo
    - B prende il turno di A
    - A prende il turno dell'assente

    Per semplicità:
    - non tocchiamo la notte in questa V2
    - lavoriamo solo con M/P
    """
    if turno_da_coprire not in ["M", "P"]:
        return None

    candidati_lavorativi = []
    for d in dipendenti:
        if d.id == dipendente_assente_id:
            continue

        if d.id in assenti_per_data.get(data_corrente, set()):
            continue

        ass = mappa.get(d.id, {}).get(data_corrente)
        if not ass:
            continue

        if ass.turno not in ["M", "P"]:
            continue

        candidati_lavorativi.append((d, ass))

    random.shuffle(candidati_lavorativi)

    for dip_a, ass_a in candidati_lavorativi:
        turno_a = ass_a.turno

        # A deve poter prendere il turno dell'assente
        # Se già ha un altro turno lavorativo nello stesso giorno,
        # lo valideremo indirettamente con lo scambio completo.
        if turno_a == turno_da_coprire:
            # In questo caso non serve lo scambio: A fa già lo stesso tipo di turno.
            # Meglio saltare, non risolve la copertura.
            continue

        # Cerco B a riposo nello stesso giorno
        candidati_riposo = []
        for d in dipendenti:
            if d.id in [dipendente_assente_id, dip_a.id]:
                continue

            if d.id in assenti_per_data.get(data_corrente, set()):
                continue

            ass_b = mappa.get(d.id, {}).get(data_corrente)
            if not ass_b:
                continue

            if ass_b.turno != "R":
                continue

            # B deve poter prendere il turno di A
            if not puo_lavorare_in_data(mappa, d.id, data_corrente, turno_a):
                continue

            candidati_riposo.append((d, ass_b))

        random.shuffle(candidati_riposo)

        for dip_b, ass_b in candidati_riposo:
            # Simulazione minima:
            # - A passa da turno_a a turno_da_coprire
            # - B passa da R a turno_a
            # Per A non serve puo_lavorare_in_data perché A già lavora quel giorno,
            # ma verifichiamo che il cambio non crei un vincolo notte adiacente.
            if turno_da_coprire == "N":
                continue

            giorno_precedente = data_corrente - timedelta(days=1)
            ass_prec_a = mappa.get(dip_a.id, {}).get(giorno_precedente)
            if ass_prec_a and ass_prec_a.turno == "N":
                continue

            if turno_da_coprire == "N":
                giorno_successivo = data_corrente + timedelta(days=1)
                ass_succ_a = mappa.get(dip_a.id, {}).get(giorno_successivo)
                if ass_succ_a and ass_succ_a.turno != "R":
                    continue

            return {
                "assegnazione_a": ass_a,
                "nuovo_turno_a": turno_da_coprire,
                "assegnazione_b": ass_b,
                "nuovo_turno_b": turno_a,
            }

    return None


def genera_turni_mese(calendario_mensile: CalendarioMensile):
    dipendenti = list(Dipendente.objects.filter(attivo=True).order_by("cognome", "nome"))

    if len(dipendenti) < 8:
        raise ValueError("Servono almeno 8 dipendenti attivi per coprire i turni giornalieri.")

    anno = calendario_mensile.anno
    mese = calendario_mensile.mese
    giorni_del_mese = calendar.monthrange(anno, mese)[1]

    AssegnazioneTurno.objects.filter(calendario=calendario_mensile).delete()

    conteggi = {
        d.id: {
            "totale_lavorati": 0,
            "M": 0,
            "P": 0,
            "N": 0,
            "R": 0,
            "F": 0,
            "A": 0,
            "L": 0,
            "X": 0,
            "weekend_lavorati": 0,
            "consecutivi": 0,
        }
        for d in dipendenti
    }

    assegnazioni_per_data = defaultdict(dict)

    assenze = Assenza.objects.filter(
        dipendente__in=dipendenti,
        data_inizio__lte=date(anno, mese, giorni_del_mese),
        data_fine__gte=date(anno, mese, 1),
    ).select_related("dipendente")

    mappa_assenze = defaultdict(dict)
    for assenza in assenze:
        giorno_corrente = max(assenza.data_inizio, date(anno, mese, 1))
        ultimo_giorno = min(assenza.data_fine, date(anno, mese, giorni_del_mese))

        while giorno_corrente <= ultimo_giorno:
            mappa_assenze[giorno_corrente][assenza.dipendente_id] = tipo_assenza_to_sigla(assenza.tipo)
            giorno_corrente += timedelta(days=1)

    offset_ciclico = random.randint(0, len(dipendenti) - 1)
    dipendenti = dipendenti[offset_ciclico:] + dipendenti[:offset_ciclico]

    def assegnato_oggi(dipendente_id, giorno_corrente):
        return dipendente_id in assegnazioni_per_data.get(giorno_corrente, {})

    def assente_oggi(dipendente_id, giorno_corrente):
        return dipendente_id in mappa_assenze.get(giorno_corrente, {})

    def ha_notte_il_giorno_prima(dipendente_id, giorno_corrente):
        giorno_precedente = giorno_corrente - timedelta(days=1)
        return assegnazioni_per_data.get(giorno_precedente, {}).get(dipendente_id) == "N"

    def aggiorna_consecutivi(giorno_corrente):
        if giorno_corrente.day == 1:
            for d in dipendenti:
                turno_oggi = assegnazioni_per_data[giorno_corrente].get(d.id)
                conteggi[d.id]["consecutivi"] = 1 if turno_oggi in ["M", "P", "N"] else 0
            return

        giorno_precedente = giorno_corrente - timedelta(days=1)
        for d in dipendenti:
            turno_oggi = assegnazioni_per_data[giorno_corrente].get(d.id)
            turno_ieri = assegnazioni_per_data[giorno_precedente].get(d.id)

            lavora_oggi = turno_oggi in ["M", "P", "N"]
            lavora_ieri = turno_ieri in ["M", "P", "N"]

            if lavora_oggi and lavora_ieri:
                conteggi[d.id]["consecutivi"] += 1
            elif lavora_oggi:
                conteggi[d.id]["consecutivi"] = 1
            else:
                conteggi[d.id]["consecutivi"] = 0

    def punteggio_generale(d, turno, giorno_corrente):
        totale = conteggi[d.id]["totale_lavorati"]
        target = target_mensile(d)
        rapporto_carico = totale / target if target else totale

        weekend_penalty = conteggi[d.id]["weekend_lavorati"] * 0.35 if e_weekend(giorno_corrente) else 0
        consecutivi_penalty = 100 if conteggi[d.id]["consecutivi"] >= MAX_TURNI_CONSECUTIVI else conteggi[d.id]["consecutivi"] * 0.4

        if turno == "N":
            return (
                conteggi[d.id]["N"],
                rapporto_carico,
                weekend_penalty,
                consecutivi_penalty,
                totale,
            )

        if d.tipo_contratto == "part_time":
            carico_diurno = conteggi[d.id]["M"] + conteggi[d.id]["P"]
            rapporto_diurno = carico_diurno / target if target else carico_diurno
            return (
                rapporto_diurno,
                conteggi[d.id][turno],
                weekend_penalty,
                consecutivi_penalty,
                totale,
            )

        return (
            rapporto_carico,
            conteggi[d.id][turno],
            conteggi[d.id]["N"] * 0.25,
            weekend_penalty,
            consecutivi_penalty,
            totale,
        )

    def ordina_candidati(candidati, turno, giorno_corrente):
        candidati = candidati[:]
        random.shuffle(candidati)
        return sorted(candidati, key=lambda d: punteggio_generale(d, turno, giorno_corrente))

    for giorno in range(1, giorni_del_mese + 1):
        data_corrente = date(anno, mese, giorno)
        fabbisogno = fabbisogno_giornaliero(data_corrente)

        for dipendente in dipendenti:
            if assente_oggi(dipendente.id, data_corrente):
                sigla_assenza = mappa_assenze[data_corrente][dipendente.id]
                assegnazioni_per_data[data_corrente][dipendente.id] = sigla_assenza
                conteggi[dipendente.id][sigla_assenza] += 1

        for dipendente in dipendenti:
            if ha_notte_il_giorno_prima(dipendente.id, data_corrente) and not assegnato_oggi(dipendente.id, data_corrente):
                assegnazioni_per_data[data_corrente][dipendente.id] = "R"
                conteggi[dipendente.id]["R"] += 1

        candidati_notte = [
            d for d in dipendenti
            if d.tipo_contratto == "full_time"
            and not assegnato_oggi(d.id, data_corrente)
            and conteggi[d.id]["consecutivi"] < MAX_TURNI_CONSECUTIVI
        ]

        candidati_notte = ordina_candidati(candidati_notte, "N", data_corrente)

        if len(candidati_notte) < fabbisogno["N"]:
            raise ValueError(
                f"Impossibile assegnare {fabbisogno['N']} notti il {data_corrente}: personale full-time insufficiente."
            )

        for scelto_notte in candidati_notte[:fabbisogno["N"]]:
            assegnazioni_per_data[data_corrente][scelto_notte.id] = "N"
            conteggi[scelto_notte.id]["N"] += 1
            conteggi[scelto_notte.id]["totale_lavorati"] += 1
            if e_weekend(data_corrente):
                conteggi[scelto_notte.id]["weekend_lavorati"] += 1

        for turno in ["M", "P"]:
            candidati = [
                d for d in dipendenti
                if not assegnato_oggi(d.id, data_corrente)
                and conteggi[d.id]["consecutivi"] < MAX_TURNI_CONSECUTIVI
            ]

            candidati = ordina_candidati(candidati, turno, data_corrente)

            if len(candidati) < fabbisogno[turno]:
                raise ValueError(
                    f"Impossibile assegnare {fabbisogno[turno]} persone al turno {turno} per il {data_corrente}: personale insufficiente."
                )

            scelti = candidati[:fabbisogno[turno]]
            for dipendente in scelti:
                assegnazioni_per_data[data_corrente][dipendente.id] = turno
                conteggi[dipendente.id][turno] += 1
                conteggi[dipendente.id]["totale_lavorati"] += 1
                if e_weekend(data_corrente):
                    conteggi[dipendente.id]["weekend_lavorati"] += 1

        for dipendente in dipendenti:
            if not assegnato_oggi(dipendente.id, data_corrente):
                assegnazioni_per_data[data_corrente][dipendente.id] = "R"
                conteggi[dipendente.id]["R"] += 1

        aggiorna_consecutivi(data_corrente)

    nuove_assegnazioni = []
    for giorno_data, assegnazioni_giornaliere in assegnazioni_per_data.items():
        for dipendente_id, turno in assegnazioni_giornaliere.items():
            nuove_assegnazioni.append(
                AssegnazioneTurno(
                    calendario=calendario_mensile,
                    dipendente_id=dipendente_id,
                    data=giorno_data,
                    turno=turno,
                )
            )

    AssegnazioneTurno.objects.bulk_create(nuove_assegnazioni)

    bilancia_weekend(calendario_mensile)
    bilancia_carico_totale(calendario_mensile)

    calendario_mensile.stato = CalendarioMensile.STATO_GENERATO
    calendario_mensile.save()

    return conteggi


def bilancia_weekend(calendario_mensile: CalendarioMensile):
    dipendenti = list(Dipendente.objects.filter(attivo=True))

    for _ in range(MAX_ITER_BILANCIAMENTO_WEEKEND):
        assegnazioni = list(
            AssegnazioneTurno.objects.filter(calendario=calendario_mensile).select_related("dipendente")
        )

        mappa = defaultdict(dict)
        for a in assegnazioni:
            mappa[a.dipendente_id][a.data] = a

        migliorato = False

        for tipo_contratto in ["full_time", "part_time"]:
            ids_gruppo = [
                d.id for d in dipendenti
                if d.tipo_contratto == tipo_contratto
            ]

            if len(ids_gruppo) < 2:
                continue

            conteggi_weekend = weekend_count_per_ids(mappa, ids_gruppo)
            max_count = max(conteggi_weekend.values())
            min_count = min(conteggi_weekend.values())

            if max_count - min_count <= 1:
                continue

            candidati_troppi = [dip_id for dip_id, c in conteggi_weekend.items() if c == max_count]
            candidati_pochi = [dip_id for dip_id, c in conteggi_weekend.items() if c == min_count]

            random.shuffle(candidati_troppi)
            random.shuffle(candidati_pochi)

            for dip_troppi in candidati_troppi:
                weekend_turni = [
                    a for a in mappa[dip_troppi].values()
                    if e_weekend(a.data) and a.turno in ["M", "P"]
                ]
                random.shuffle(weekend_turni)

                for ass_weekend in weekend_turni:
                    data_corrente = ass_weekend.data
                    turno_da_spostare = ass_weekend.turno

                    for dip_pochi in candidati_pochi:
                        if dip_pochi == dip_troppi:
                            continue

                        if puo_lavorare_in_data(mappa, dip_pochi, data_corrente, turno_da_spostare):
                            ass_ricevente = mappa[dip_pochi][data_corrente]

                            ass_weekend.turno = "R"
                            ass_ricevente.turno = turno_da_spostare

                            ass_weekend.save(update_fields=["turno"])
                            ass_ricevente.save(update_fields=["turno"])

                            migliorato = True
                            break

                    if migliorato:
                        break

                if migliorato:
                    break

            if migliorato:
                break

            for dip_troppi in candidati_troppi:
                weekend_turni = [
                    a for a in mappa[dip_troppi].values()
                    if e_weekend(a.data) and a.turno in ["M", "P"]
                ]
                random.shuffle(weekend_turni)

                for ass_weekend in weekend_turni:
                    turno_target = ass_weekend.turno
                    data_weekend = ass_weekend.data

                    for dip_pochi in candidati_pochi:
                        if dip_pochi == dip_troppi:
                            continue

                        feriali_stesso_turno = [
                            a for a in mappa[dip_pochi].values()
                            if (not e_weekend(a.data)) and a.turno == turno_target
                        ]
                        random.shuffle(feriali_stesso_turno)

                        for ass_feriale in feriali_stesso_turno:
                            data_feriale = ass_feriale.data

                            ass_troppi_feriale = mappa[dip_troppi][data_feriale]
                            if ass_troppi_feriale.turno != "R":
                                continue

                            if not puo_lavorare_in_data(mappa, dip_troppi, data_feriale, turno_target):
                                continue

                            ass_pochi_weekend = mappa[dip_pochi][data_weekend]
                            if ass_pochi_weekend.turno != "R":
                                continue

                            if not puo_lavorare_in_data(mappa, dip_pochi, data_weekend, turno_target):
                                continue

                            ass_weekend.turno = "R"
                            ass_troppi_feriale.turno = turno_target

                            ass_feriale.turno = "R"
                            ass_pochi_weekend.turno = turno_target

                            ass_weekend.save(update_fields=["turno"])
                            ass_troppi_feriale.save(update_fields=["turno"])
                            ass_feriale.save(update_fields=["turno"])
                            ass_pochi_weekend.save(update_fields=["turno"])

                            migliorato = True
                            break

                        if migliorato:
                            break

                    if migliorato:
                        break

                if migliorato:
                    break

            if migliorato:
                break

        if not migliorato:
            break


def bilancia_carico_totale(calendario_mensile: CalendarioMensile):
    dipendenti = list(Dipendente.objects.filter(attivo=True))

    for _ in range(MAX_ITER_BILANCIAMENTO_CARICO):
        assegnazioni = list(
            AssegnazioneTurno.objects.filter(calendario=calendario_mensile).select_related("dipendente")
        )

        mappa = defaultdict(dict)
        for a in assegnazioni:
            mappa[a.dipendente_id][a.data] = a

        migliorato = False

        for tipo_contratto in ["full_time", "part_time"]:
            ids_gruppo = [
                d.id for d in dipendenti
                if d.tipo_contratto == tipo_contratto
            ]

            if len(ids_gruppo) < 2:
                continue

            totali = totale_turni_per_ids(mappa, ids_gruppo)
            max_count = max(totali.values())
            min_count = min(totali.values())

            if max_count - min_count <= 1:
                continue

            candidati_alti = [dip_id for dip_id, c in totali.items() if c == max_count]
            candidati_bassi = [dip_id for dip_id, c in totali.items() if c == min_count]

            random.shuffle(candidati_alti)
            random.shuffle(candidati_bassi)

            for dip_alto in candidati_alti:
                turni_cedibili = [
                    a for a in mappa[dip_alto].values()
                    if a.turno in ["M", "P"]
                ]
                turni_cedibili.sort(key=lambda a: (e_weekend(a.data), random.random()))

                for ass_da_cedere in turni_cedibili:
                    data_corrente = ass_da_cedere.data
                    turno_da_cedere = ass_da_cedere.turno

                    for dip_basso in candidati_bassi:
                        if dip_basso == dip_alto:
                            continue

                        if puo_lavorare_in_data(mappa, dip_basso, data_corrente, turno_da_cedere):
                            ass_ricevente = mappa[dip_basso][data_corrente]

                            ass_da_cedere.turno = "R"
                            ass_ricevente.turno = turno_da_cedere

                            ass_da_cedere.save(update_fields=["turno"])
                            ass_ricevente.save(update_fields=["turno"])

                            migliorato = True
                            break

                    if migliorato:
                        break

                if migliorato:
                    break

            if migliorato:
                break

        if not migliorato:
            break


def ripianifica_calendario(calendario_mensile: CalendarioMensile):
    """
    V2 migliorata:
    - legge le assenze già inserite
    - aggiorna solo i giorni colpiti
    - non rigenera tutto il mese
    - cerca copertura nello stesso giorno con una piccola catena di scambi
    - se non trova soluzione, annulla tutto
    """
    with transaction.atomic():
        dipendenti = list(Dipendente.objects.filter(attivo=True).order_by("cognome", "nome"))

        anno = calendario_mensile.anno
        mese = calendario_mensile.mese
        giorni_del_mese = calendar.monthrange(anno, mese)[1]

        assegnazioni = list(
            AssegnazioneTurno.objects.filter(calendario=calendario_mensile).select_related("dipendente")
        )

        if not assegnazioni:
            raise ValueError("Il calendario non contiene ancora assegnazioni da ripianificare.")

        mappa = defaultdict(dict)
        for a in assegnazioni:
            mappa[a.dipendente_id][a.data] = a

        assenze = Assenza.objects.filter(
            dipendente__in=dipendenti,
            data_inizio__lte=date(anno, mese, giorni_del_mese),
            data_fine__gte=date(anno, mese, 1),
        ).select_related("dipendente")

        assenti_per_data = defaultdict(set)
        sigla_per_data = defaultdict(dict)

        for assenza in assenze:
            giorno_corrente = max(assenza.data_inizio, date(anno, mese, 1))
            ultimo_giorno = min(assenza.data_fine, date(anno, mese, giorni_del_mese))

            while giorno_corrente <= ultimo_giorno:
                assenti_per_data[giorno_corrente].add(assenza.dipendente_id)
                sigla_per_data[giorno_corrente][assenza.dipendente_id] = tipo_assenza_to_sigla(assenza.tipo)
                giorno_corrente += timedelta(days=1)

        assenze_aggiornate = 0
        coperture_sistemate = 0
        scambi_effettuati = 0

        giorni_ordinati = sorted(sigla_per_data.keys())

        for data_corrente in giorni_ordinati:
            dipendenti_assenti = sigla_per_data[data_corrente]

            for dip_id, sigla_assenza in dipendenti_assenti.items():
                ass = mappa.get(dip_id, {}).get(data_corrente)
                if not ass:
                    continue

                if ass.turno == sigla_assenza:
                    continue

                turno_precedente = ass.turno

                # Trasforma il turno corrente in assenza
                ass.turno = sigla_assenza
                ass.save(update_fields=["turno"])
                assenze_aggiornate += 1

                # Se prima non lavorava, non devo coprire nulla
                if turno_precedente not in ["M", "P", "N"]:
                    continue

                # Cerca una catena locale di copertura nello stesso giorno
                soluzione = trova_catena_copertura(
                    mappa=mappa,
                    dipendenti=dipendenti,
                    assenti_per_data=assenti_per_data,
                    data_corrente=data_corrente,
                    turno_da_coprire=turno_precedente,
                    dipendente_escluso_id=dip_id,
                    profondita_max=3,
                )

                if soluzione is None:
                    raise ValueError(
                        f"Impossibile coprire l'assenza del {data_corrente.strftime('%d/%m/%Y')} "
                        f"senza modificare altre parti del calendario."
                    )

                # Applica la soluzione
                # Se la catena è lunga 1, è una sostituzione diretta.
                # Se è >1, ci sono scambi locali.
                for ass_obj, nuovo_turno in soluzione:
                    if ass_obj.turno != nuovo_turno:
                        ass_obj.turno = nuovo_turno
                        ass_obj.save(update_fields=["turno"])

                coperture_sistemate += 1
                if len(soluzione) > 1:
                    scambi_effettuati += (len(soluzione) - 1)

        return {
            "assenze_aggiornate": assenze_aggiornate,
            "coperture_sistemate": coperture_sistemate,
            "scambi_effettuati": scambi_effettuati,
        }