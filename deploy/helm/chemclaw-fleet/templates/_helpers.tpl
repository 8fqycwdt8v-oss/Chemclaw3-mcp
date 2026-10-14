{{- /*
Names are fixed, not release-prefixed: Chemclaw3 dials `http://chemclaw-mcp-<name>:<port>/mcp` and
reads `CHEMCLAW_<NAME>_TOKEN` from the shared Secret, so the chart can be installed under any
release name and in any namespace without moving either. The two `chemclaw-fleet.*` names below
are the interface a parent chart uses to derive the same strings from one `servers.<name>.port`.

Every value a template interpolates as a scalar goes through `quote` (or `toYaml`/`int`), never
bare: a value with a newline in it would otherwise add keys to the object around it, which is how
`hostNetwork` or an `envFrom` would reach a pod. `values.schema.json` also constrains the strings,
but this is the layer that holds when the schema is skipped.
*/ -}}
{{- define "chemclaw-fleet.service" -}}
chemclaw-mcp-{{ . }}
{{- end -}}

{{- define "chemclaw-fleet.tokenEnv" -}}
CHEMCLAW_{{ . | upper | replace "-" "_" }}_TOKEN
{{- end -}}

{{- /* The metadata labels, whose `name` is the one selector the Deployment, Service, PDB, spread
constraint, NetworkPolicy and ServiceMonitor share. A drift in any one silently exempts the workload
(for the NetworkPolicy, from default-deny egress), so the name is derived from one function. */ -}}
{{- define "chemclaw-fleet.labels" -}}
app.kubernetes.io/name: {{ include "chemclaw-fleet.service" . | quote }}
app.kubernetes.io/part-of: chemclaw3
{{- end -}}

{{- /* The server's settings: `defaults` overlaid by its own entry, as YAML. Only truthy keys may
override (mergo skips zero values), so every boolean here defaults to false. Called with
(dict "root" $ "name" <name>). */ -}}
{{- define "chemclaw-fleet.config" -}}
{{- $server := get .root.Values.servers .name -}}
{{- toYaml (mergeOverwrite (deepCopy .root.Values.defaults) (deepCopy $server)) -}}
{{- end -}}

{{- /* `registry/prefix+name@digest` or `:tag`. `latest` and an empty tag are refused here, not by
the schema: a moving tag cannot be rolled back to known bytes. */ -}}
{{- define "chemclaw-fleet.image" -}}
{{- $i := .image -}}
{{- $repo := printf "%s/%s%s" $i.registry $i.namePrefix .name -}}
{{- if $i.digest -}}
{{- $repo }}@{{ $i.digest }}
{{- else -}}
{{- if or (not $i.tag) (eq (toString $i.tag) "latest") -}}
{{- fail (printf "servers.%s: image.tag must be a fixed, non-latest tag or a digest" .name) -}}
{{- end -}}
{{- $repo }}:{{ $i.tag }}
{{- end -}}
{{- end -}}

{{- /* Cross-server rules a per-field schema cannot state. Run from deployment.yaml, which every
render includes. */ -}}
{{- define "chemclaw-fleet.validate" -}}
{{- $ports := dict -}}
{{- range $name, $_ := .Values.servers -}}
{{- if not (regexMatch "^[a-z][a-z0-9]*$" $name) -}}
{{- fail (printf "servers.%s: a server name is lower-case letters and digits, as its directory under servers/ is" $name) -}}
{{- end -}}
{{- $c := include "chemclaw-fleet.config" (dict "root" $ "name" $name) | fromYaml -}}
{{- $port := toString (int $c.port) -}}
{{- if hasKey $ports $port -}}
{{- fail (printf "servers.%s and servers.%s both claim port %s" (get $ports $port) $name $port) -}}
{{- end -}}
{{- $noop := set $ports $port $name -}}
{{- if lt (int $c.maxReplicas) (int $c.replicas) -}}
{{- fail (printf "servers.%s: maxReplicas %d is below replicas %d, so the autoscaler could never hold its own floor" $name (int $c.maxReplicas) (int $c.replicas)) -}}
{{- end -}}
{{- end -}}
{{- end -}}
