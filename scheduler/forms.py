from django import forms
from .models import Dipendente, CalendarioMensile,Assenza


import re
from datetime import datetime


class DipendenteForm(forms.ModelForm):
    class Meta:
        model = Dipendente
        fields = [
            "utente",
            "nome",
            "cognome",
            "data_nascita",
            "livello",
            "tipo_contratto",
            "attivo",
        ]
        widgets = {
            "data_nascita": forms.DateInput(attrs={"type": "date"}),
        }


class CalendarioMensileForm(forms.ModelForm):
    class Meta:
        model = CalendarioMensile
        fields = ["mese", "anno"]


class AssenzaForm(forms.ModelForm):
    data_inizio = forms.CharField(
        label="Data inizio",
        widget=forms.TextInput(attrs={"placeholder": "gg/mm/aaaa"})
    )
    data_fine = forms.CharField(
        label="Data fine",
        widget=forms.TextInput(attrs={"placeholder": "gg/mm/aaaa"})
    )

    class Meta:
        model = Assenza
        fields = ["dipendente", "tipo", "data_inizio", "data_fine", "note"]
        widgets = {
            "note": forms.Textarea(attrs={"rows": 3}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        # Se sto modificando un'assenza già esistente, mostro le date nel formato gg/mm/aaaa
        if self.instance and self.instance.pk:
            if self.instance.data_inizio:
                self.fields["data_inizio"].initial = self.instance.data_inizio.strftime("%d/%m/%Y")
            if self.instance.data_fine:
                self.fields["data_fine"].initial = self.instance.data_fine.strftime("%d/%m/%Y")

    def _parse_data_con_errori_precisi(self, valore, nome_campo):
        valore = (valore or "").strip()

        if not valore:
            raise forms.ValidationError(f"Inserire la {nome_campo}.")

        parti = valore.split("/")

        if len(parti) != 3:
            raise forms.ValidationError(
                f"La {nome_campo} deve essere nel formato gg/mm/aaaa."
            )

        giorno, mese, anno = [p.strip() for p in parti]

        if not giorno:
            raise forms.ValidationError(f"Inserire il giorno della {nome_campo}.")
        if not mese:
            raise forms.ValidationError(f"Inserire il mese della {nome_campo}.")
        if not anno:
            raise forms.ValidationError(f"Inserire l'anno della {nome_campo}.")

        if not giorno.isdigit():
            raise forms.ValidationError(f"Il giorno della {nome_campo} deve contenere solo numeri.")
        if not mese.isdigit():
            raise forms.ValidationError(f"Il mese della {nome_campo} deve contenere solo numeri.")
        if not anno.isdigit():
            raise forms.ValidationError(f"L'anno della {nome_campo} deve contenere solo numeri.")

        if len(anno) != 4:
            raise forms.ValidationError(f"L'anno della {nome_campo} deve avere 4 cifre.")

        try:
            return datetime.strptime(valore, "%d/%m/%Y").date()
        except ValueError:
            raise forms.ValidationError(f"La {nome_campo} non è valida.")

    def clean_data_inizio(self):
        valore = self.cleaned_data.get("data_inizio")
        return self._parse_data_con_errori_precisi(valore, "data di inizio")

    def clean_data_fine(self):
        valore = self.cleaned_data.get("data_fine")
        return self._parse_data_con_errori_precisi(valore, "data di fine")

    def clean(self):
        cleaned_data = super().clean()
        data_inizio = cleaned_data.get("data_inizio")
        data_fine = cleaned_data.get("data_fine")

        if data_inizio and data_fine and data_fine < data_inizio:
            raise forms.ValidationError(
                "La data di fine non può essere precedente alla data di inizio."
            )

        return cleaned_data