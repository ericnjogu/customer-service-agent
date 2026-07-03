{{- define "customer-support.name" -}}
{{- .Chart.Name | trunc 63 | trimSuffix "-" }}
{{- end }}

{{- define "customer-support.fullname" -}}
{{- printf "%s-%s" .Release.Name (include "customer-support.name" .) | trunc 63 | trimSuffix "-" }}
{{- end }}

{{- define "customer-support.labels" -}}
app.kubernetes.io/name: {{ include "customer-support.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end }}

