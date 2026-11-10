// Declarative pipeline: parameterised, Dockerised JMeter, quality gate, AI-assisted analysis, Grafana-tagged runs.
def runStandalone() {
  return sh(returnStatus: true, script: '''
    mkdir -p results && chmod 777 results
    docker run --rm -v "$PWD/results:/opt/omp/results" \\
      -e MODE=standalone -e ENV -e PROFILE -e TESTRUN -e USERS -e DURATION -e RAMPUP \\
      -e OAUTH_CLIENT_SECRET -e JWT_SECRET \\
      -e RUN_AI_ANALYSIS -e ANNOTATE_GRAFANA -e SAVE_HISTORY -e ANTHROPIC_API_KEY -e AI_MODE -e AI_MODEL \\
      $IMAGE''')
}

pipeline {
  agent { label 'perf-agent' }            // dedicated agent - never the controller, never shared with builds

  parameters {
    choice(name: 'ENV',     choices: ['qa', 'perf', 'local'], description: 'Target environment (config/env/<ENV>.properties)')
    choice(name: 'PROFILE', choices: ['smoke', 'load', 'stress', 'spike', 'soak', 'protocols'], description: 'Load profile')
    string(name: 'USERS',    defaultValue: '', description: 'Override total users (blank = profile value)')
    string(name: 'DURATION', defaultValue: '', description: 'Override total seconds incl. ramp-up')
    string(name: 'RAMPUP',   defaultValue: '', description: 'Override ramp-up seconds')
    booleanParam(name: 'DISTRIBUTED', defaultValue: false, description: 'Run on Kubernetes workers instead of one container')
    string(name: 'WORKERS',  defaultValue: '3', description: 'Workers when DISTRIBUTED')
    booleanParam(name: 'AI_ANALYSIS', defaultValue: true,  description: 'Statistical + rule-based analysis (no API key needed)')
    booleanParam(name: 'AI_USE_LLM',  defaultValue: false, description: 'Refine the analysis with an LLM (needs credential anthropic-api-key; aggregated, redacted data only)')
  }
  triggers { cron('H 1 * * 1-5') }        // nightly regression (uses parameter defaults)
  options  { timeout(time: 12, unit: 'HOURS'); timestamps(); buildDiscarder(logRotator(numToKeepStr: '30')) }
  environment {
    IMAGE   = "registry.example.com/perf/omp-jmeter:${env.BUILD_NUMBER}"
    TESTRUN = "${env.JOB_BASE_NAME}-${env.BUILD_NUMBER}"      // tags the run in Grafana/Influx
    RUN_AI_ANALYSIS = "${params.AI_ANALYSIS}"
    ANNOTATE_GRAFANA = "true"
  }

  stages {
    stage('Build image') {
      steps { sh 'docker build -t $IMAGE . && docker push $IMAGE || true' }
    }
    stage('Pre-flight') {
      when { expression { !params.DISTRIBUTED } }
      steps { sh 'docker run --rm -e ENV=$ENV --entrypoint /opt/omp/scripts/preflight.sh $IMAGE' }
    }
    stage('Run JMeter') {
      steps {
        withCredentials([string(credentialsId: 'omp-oauth-secret', variable: 'OAUTH_CLIENT_SECRET'),
                         string(credentialsId: 'omp-jwt-secret',   variable: 'JWT_SECRET')]) {
          script {
            // exit codes: 0 pass | 1 SLA breach | 2 regression warning (the AI step can never change these)
            def rc
            if (params.DISTRIBUTED) {
              withCredentials([file(credentialsId: 'kubeconfig', variable: 'KUBECONFIG')]) {
                rc = sh(returnStatus: true, script: 'WORKERS=$WORKERS ENV=$ENV PROFILE=$PROFILE TESTRUN=$TESTRUN OUT=results ./k8s/run_distributed.sh')
              }
            } else if (params.AI_USE_LLM) {
              withCredentials([string(credentialsId: 'anthropic-api-key', variable: 'ANTHROPIC_API_KEY')]) { rc = runStandalone() }
            } else {
              rc = runStandalone()
            }
            if (rc == 1) { error('Performance SLA breached (see Quality Gate output / JUnit tab / AI analysis)') }
            if (rc == 2) { unstable('Performance regression vs baseline') }
            if (rc != 0 && rc != 1 && rc != 2) { error("Run failed with exit code ${rc}") }
          }
        }
      }
    }
  }
  post {
    always {
      archiveArtifacts artifacts: 'results/**', allowEmptyArchive: true, fingerprint: true
      junit allowEmptyResults: true, testResults: 'results/junit.xml'            // per-transaction pass/fail
      publishHTML(target: [reportDir: 'results/report', reportFiles: 'index.html', reportName: 'JMeter Dashboard', keepAll: true, allowMissing: true])
      publishHTML(target: [reportDir: 'results/ai', reportFiles: 'ai_analysis.html', reportName: 'AI Analysis', keepAll: true, allowMissing: true])
    }
    failure  { script { notify('danger',  'Perf FAILED') } }
    unstable { script { notify('warning', 'Perf regression') } }
  }
}

def notify(color, title) {
  def ai = fileExists('results/ai/summary.txt') ? readFile('results/ai/summary.txt').take(700) : ''
  slackSend channel: '#perf-alerts', color: color, message: "${title}: ${env.JOB_NAME} #${env.BUILD_NUMBER} ${env.BUILD_URL}\n${ai}"
}
