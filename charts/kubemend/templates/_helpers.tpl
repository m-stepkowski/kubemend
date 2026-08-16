{{- define "kubemend.name" -}}
{{- .Chart.Name -}}
{{- end -}}

{{- define "kubemend.fullname" -}}
{{- .Release.Name -}}
{{- end -}}

{{- define "kubemend.labels" -}}
app.kubernetes.io/name: {{ include "kubemend.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end -}}

{{- define "kubemend.image" -}}
{{- printf "%s:%s" .Values.image.repository (.Values.image.tag | default .Chart.AppVersion) -}}
{{- end -}}

{{/*
Shared read-only rule list, mirroring lab/bootstrap/rbac.yaml's ClusterRole
exactly (ARCHITECTURE.md §8) — zero verbs on secrets, structural rather than
a convention. Defined once so the namespace-scoped Role and cluster-scoped
ClusterRole variants in reader-rbac.yaml can never render different rules
by accident; only the resource kind differs between them.
*/}}
{{- define "kubemend.readerRules" -}}
- apiGroups: [""]
  resources: [pods, pods/log, services, configmaps, events, resourcequotas, namespaces]
  verbs: [get, list, watch]
- apiGroups: ["apps"]
  resources: [deployments, statefulsets, replicasets, daemonsets]
  verbs: [get, list, watch]
- apiGroups: ["autoscaling"]
  resources: [horizontalpodautoscalers]
  verbs: [get, list, watch]
- apiGroups: ["networking.k8s.io"]
  resources: [ingresses]
  verbs: [get, list, watch]
{{- end -}}
