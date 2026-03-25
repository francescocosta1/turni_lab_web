# Attivo il pannello admin
from django.contrib import admin
from .models import Dipendente, CalendarioMensile, AssegnazioneTurno


admin.site.register(Dipendente)
admin.site.register(CalendarioMensile)
admin.site.register(AssegnazioneTurno)
