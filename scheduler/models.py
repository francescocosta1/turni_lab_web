from django.db import models
from django.contrib.auth.models import User


class Dipendente(models.Model):
    LIVELLI = [
        ("senior", "Senior"),
        ("junior", "Junior"),
    ]

    TIPI_CONTRATTO = [
        ("full_time", "Full-time"),
        ("part_time", "Part-time"),
    ]

    utente = models.OneToOneField(User, on_delete=models.SET_NULL, null=True, blank=True)
    nome = models.CharField(max_length=100)
    cognome = models.CharField(max_length=100)
    data_nascita = models.DateField(null=True, blank=True)
    livello = models.CharField(max_length=10, choices=LIVELLI)
    tipo_contratto = models.CharField(
        max_length=20,
        choices=TIPI_CONTRATTO,
        default="full_time"
    )
    attivo = models.BooleanField(default=True)

    class Meta:
        ordering = ["cognome", "nome"]
        verbose_name = "Dipendente"
        verbose_name_plural = "Dipendenti"

    def __str__(self):
        return f"{self.cognome} {self.nome}"


class CalendarioMensile(models.Model):
    STATO_BOZZA = "bozza"
    STATO_GENERATO = "generato"

    STATI = [
        (STATO_BOZZA, "Bozza"),
        (STATO_GENERATO, "Generato"),
    ]

    anno = models.IntegerField()
    mese = models.IntegerField()
    stato = models.CharField(max_length=20, choices=STATI, default=STATO_BOZZA)
    creato_il = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ("anno", "mese")
        ordering = ["-anno", "-mese"]
        verbose_name = "Calendario mensile"
        verbose_name_plural = "Calendari mensili"

    def __str__(self):
        return f"{self.mese:02d}/{self.anno}"


class AssegnazioneTurno(models.Model):
    TURNI = [
        ("M", "Mattina"),
        ("P", "Pomeriggio"),
        ("N", "Notte"),
        ("R", "Riposo"),
        ("F", "Ferie"),
        ("A", "Assenza"),
        ("L", "Malattia"),
        ("X", "Permesso"),
    ]

    calendario = models.ForeignKey(
        CalendarioMensile,
        on_delete=models.CASCADE,
        related_name="assegnazioni"
    )
    dipendente = models.ForeignKey(
        Dipendente,
        on_delete=models.CASCADE,
        related_name="assegnazioni"
    )
    data = models.DateField()
    turno = models.CharField(max_length=1, choices=TURNI)

    class Meta:
        unique_together = ("dipendente", "data", "calendario")
        ordering = ["data", "dipendente__cognome"]
        verbose_name = "Assegnazione turno"
        verbose_name_plural = "Assegnazioni turni"

    def __str__(self):
        return f"{self.data} - {self.dipendente} - {self.turno}"


class Assenza(models.Model):
    TIPI = [
        ("ferie", "Ferie"),
        ("malattia", "Malattia"),
        ("permesso", "Permesso"),
        ("assenza", "Assenza"),
    ]

    dipendente = models.ForeignKey(
        Dipendente,
        on_delete=models.CASCADE,
        related_name="assenze"
    )
    tipo = models.CharField(max_length=20, choices=TIPI)
    data_inizio = models.DateField()
    data_fine = models.DateField()
    note = models.TextField(blank=True)

    class Meta:
        ordering = ["-data_inizio", "dipendente__cognome"]
        verbose_name = "Assenza"
        verbose_name_plural = "Assenze"

    def __str__(self):
        return f"{self.dipendente} - {self.get_tipo_display()} ({self.data_inizio} - {self.data_fine})"