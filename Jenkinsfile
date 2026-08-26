// Delivery for the MCP tool fleet: build every server's image, publish it, and report the digests.
//
// **Nothing has ever built these seven images.** `.github/workflows/ci.yml` runs the suite, the
// no-network suite and the fleet invariants — all of which are true of the *source* — and stops
// there. Seven `Containerfile`s were exercised by nothing, which for a repository whose central
// promise is "answers from data baked into the image" is the least checked thing in the tree.
//
// The server list is **derived from `servers/*/Containerfile`**, never written down here. A
// hand-kept list is how Chemclaw3's image workflow came to smoke a component that had not existed
// for months; `tests/test_delivery.py` asserts this file still derives rather than enumerates.
//
// This publishes and reports. It does not deploy: no server here ships a chart, so a rollout is
// `deploy/jenkins/targets/openshift.sh` in the Chemclaw3 checkout, reading the digests below out of
// a release descriptor. See that repository's `deploy/jenkins/README.md`.
pipeline {
  agent any

  options {
    timestamps()
    disableConcurrentBuilds()
    buildDiscarder(logRotator(numToKeepStr: '30', artifactNumToKeepStr: '30'))
    timeout(time: 120, unit: 'MINUTES')
  }

  parameters {
    string(name: 'IMAGE_REGISTRY', defaultValue: '',
           description: 'Registry and org, e.g. image-registry.openshift-image-registry.svc:5000/chemclaw. Empty = build only.')
    string(name: 'SERVERS', defaultValue: '',
           description: 'Space-separated subset to build. Empty = every server with a Containerfile. `calc` carries the physics and is the slow one.')
    choice(name: 'IMAGE_BUILDER', choices: ['autodetect', 'buildah', 'podman', 'kaniko', 'docker'],
           description: 'How to build. OpenShift agents get no Docker socket.')
    booleanParam(name: 'RUN_GATE', defaultValue: false,
                 description: 'Run `make check` and the no-network suite here too. Off because GitHub Actions is the gate.')
    booleanParam(name: 'DRY_RUN', defaultValue: true,
                 description: 'Build and verify without publishing. Default true, deliberately.')
    string(name: 'REGISTRY_CREDENTIALS_ID', defaultValue: 'chemclaw-registry',
           description: 'Jenkins username/password credential for the registry.')
    string(name: 'CHEMCLAW3_REPO', defaultValue: 'https://github.com/8fqycwdt8v-oss/Chemclaw3.git',
           description: 'Where the shared build/publish library lives (deploy/jenkins/lib). One implementation, four repositories.')
    string(name: 'CHEMCLAW3_BRANCH', defaultValue: 'main', description: 'Branch to take that library from.')
  }

  environment {
    IMAGE_BUILDER = "${params.IMAGE_BUILDER == 'autodetect' ? '' : params.IMAGE_BUILDER}"
  }

  stages {
    stage('Preflight') {
      steps {
        script {
          env.REVISION = sh(script: 'git rev-parse HEAD', returnStdout: true).trim()
          def discovered = sh(script: "ls -d servers/*/Containerfile | cut -d/ -f2", returnStdout: true).trim().split('\n')
          env.SERVER_LIST = (params.SERVERS ? params.SERVERS.split(/\s+/) : discovered).join(' ')
          echo "revision ${env.REVISION}\nservers  ${env.SERVER_LIST}"
        }
        // The shared library rather than a fourth copy of `build_and_push`. Vendoring it here is
        // what makes the digest-not-tag rule one implementation instead of four that drift.
        sh """
          rm -rf .jenkins-lib
          git clone --depth 1 --branch '${params.CHEMCLAW3_BRANCH}' --filter=blob:none --sparse \
            '${params.CHEMCLAW3_REPO}' .jenkins-lib
          cd .jenkins-lib && git sparse-checkout set deploy/jenkins/lib
        """
      }
    }

    stage('Gate') {
      when { expression { params.RUN_GATE } }
      steps {
        sh 'uv sync --frozen'
        sh 'make check'
        // The strongest form of the no-egress claim, and the one that does not trust our own code.
        sh 'make offline-run'
      }
    }

    stage('Build and publish every server') {
      steps {
        withCredentials([usernamePassword(credentialsId: params.REGISTRY_CREDENTIALS_ID,
                                          usernameVariable: 'REGISTRY_USER', passwordVariable: 'REGISTRY_PASSWORD')]) {
          script {
            def digests = [:]
            for (name in env.SERVER_LIST.split(' ')) {
              def ref = params.IMAGE_REGISTRY ? "${params.IMAGE_REGISTRY}/chemclaw-mcp-${name}:${env.REVISION.take(12)}"
                                              : "chemclaw-mcp-${name}:${env.REVISION.take(12)}"
              if (params.DRY_RUN || !params.IMAGE_REGISTRY) {
                sh """
                  set -euo pipefail
                  . .jenkins-lib/deploy/jenkins/lib/image.sh
                  builder="\$(detect_builder)"
                  case "\${builder}" in
                    kaniko) echo "kaniko cannot build without pushing — nothing to verify in a dry run" ;;
                    *) "\${builder}" build -f servers/${name}/Containerfile \
                         --build-arg CHEMCLAW_REVISION=${env.REVISION} -t '${ref}' . ;;
                  esac
                """
              } else {
                digests[name] = sh(returnStdout: true, script: """
                  set -euo pipefail
                  . .jenkins-lib/deploy/jenkins/lib/registry-login.sh
                  . .jenkins-lib/deploy/jenkins/lib/image.sh
                  registry_login '${params.IMAGE_REGISTRY}'
                  build_and_push servers/${name}/Containerfile . '${ref}' \
                    --build-arg CHEMCLAW_REVISION=${env.REVISION}
                """).trim()
              }
              env["IMAGE_REF_${name}"] = ref
            }
            env.DIGEST_LINES = digests.collect { name, digest -> "${name}=${digest}" }.join('\n')
          }
        }
      }
    }

    // What only a running image proves, and what the fleet's own tests cannot: that the server
    // answers on its declared port with only what the image carries. `MCP_SERVER_REVISION` reaching
    // `/healthz` is the same check `tests/test_fleet.py` makes of the *file* — this makes it of the
    // *build*, which is where Chemclaw3's revision field read "unknown" for eight months.
    stage('Verify every image answers') {
      when { expression { params.IMAGE_BUILDER != 'kaniko' } }
      steps {
        script {
          for (name in env.SERVER_LIST.split(' ')) {
            // Port and credential env both come out of the manifest, because the manifest is the
            // contract: a server whose token_env is not `CHEMCLAW_<NAME>_TOKEN` would otherwise be
            // started with no credential and pass this stage by refusing everything.
            def port = sh(returnStdout: true, script:
              "grep -oE ':[0-9]{4}/mcp' servers/${name}/connector.yaml | head -1 | tr -dc '0-9'").trim()
            def tokenEnv = sh(returnStdout: true, script:
              "grep -oE 'token_env: *[A-Z0-9_]+' servers/${name}/connector.yaml | head -1 | awk '{print \$2}'").trim()
            sh """
              set -euo pipefail
              runner="\$(command -v podman || command -v docker)"
              cid="\$("\${runner}" run -d -p 127.0.0.1:${port}:${port} \
                -e ${tokenEnv}=verify-token '${env["IMAGE_REF_${name}"]}')"
              trap '"\${runner}" rm -f "\${cid}" >/dev/null 2>&1 || true' EXIT
              for _ in \$(seq 1 60); do
                curl -fsS "http://127.0.0.1:${port}/healthz" >/dev/null 2>&1 && break || sleep 1
              done
              body="\$(curl -fsS --max-time 10 "http://127.0.0.1:${port}/healthz")"
              echo "${name}: \${body}"
              case "\${body}" in
                *"${env.REVISION}"*) : ;;
                *) echo "${name}: /healthz does not report this build's revision" >&2; exit 1 ;;
              esac
              # Bearer is enforced on /mcp itself, and a mounted MCP app bypasses the enclosing
              # app's dependencies — the trap CLAUDE.md names. An unauthenticated call must be
              # refused by the running server, not by a route decorator somebody read in the source.
              code="\$(curl -s -o /dev/null -w '%{http_code}' --max-time 10 -X POST \
                -H 'content-type: application/json' -d '{}' "http://127.0.0.1:${port}/mcp")"
              case "\${code}" in
                401|403) echo "${name}: /mcp refuses an unauthenticated call (HTTP \${code})" ;;
                *) echo "${name}: /mcp answered \${code} with no credential — auth is not enforced" >&2; exit 1 ;;
              esac
            """
          }
        }
      }
    }

    stage('Report the digests') {
      when { expression { !params.DRY_RUN && params.IMAGE_REGISTRY != '' } }
      steps {
        script {
          writeFile file: 'mcp-digests.txt', text: env.DIGEST_LINES + '\n'
          archiveArtifacts artifacts: 'mcp-digests.txt', fingerprint: true
          echo "Paste into the Chemclaw3 release job's MCP_DIGESTS parameter:\n${env.DIGEST_LINES}"
        }
      }
    }
  }

  post { always { sh 'rm -rf .jenkins-lib' } }
}
