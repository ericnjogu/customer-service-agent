{{- define "customer-service.name" -}}
{{- .Chart.Name | trunc 63 | trimSuffix "-" }}
{{- end }}

{{- define "customer-service.fullname" -}}
{{- printf "%s-%s" .Release.Name (include "customer-service.name" .) | trunc 63 | trimSuffix "-" }}
{{- end }}

{{- define "customer-service.labels" -}}
app.kubernetes.io/name: {{ include "customer-service.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end }}

{{- define "customer-service.image" -}}
{{- if .digest -}}
{{ printf "%s@%s" .repository .digest }}
{{- else -}}
{{ printf "%s:%s" .repository .tag }}
{{- end -}}
{{- end }}
