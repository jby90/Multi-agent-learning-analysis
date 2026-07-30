param(
    [string]$Server = "47.116.46.81",
    [string]$User = "admin",
    [string]$Branch = "agent/frontend-apple-refactor",
    [string]$KeyPath = "$HOME/.ssh/id_rsa"
)

$ErrorActionPreference = "Stop"

if ($Branch -notmatch '^[A-Za-z0-9._/-]+$') {
    throw "Branch contains unsupported characters: $Branch"
}

$repoRoot = (git rev-parse --show-toplevel).Trim()
if (-not $repoRoot) {
    throw "Run this script inside the project Git repository."
}
Set-Location $repoRoot

if (git status --porcelain --untracked-files=no) {
    throw "Tracked files contain uncommitted changes. Commit them before publishing."
}

$target = (git rev-parse $Branch).Trim()
$remote = "${User}@${Server}"
$remoteRepo = "/opt/multiagent/Multi-agent-learning-analysis"
$remoteBundle = "/opt/multiagent/update.bundle"
$previous = (& ssh -i $KeyPath $remote "cd '$remoteRepo' && git rev-parse HEAD").Trim()

git merge-base --is-ancestor $previous $target
if ($LASTEXITCODE -ne 0) {
    throw "Server commit $previous is not an ancestor of $target; refusing a non-fast-forward publish."
}

$bundlePath = Join-Path ([System.IO.Path]::GetTempPath()) "multiagent-update-$target.bundle"
try {
    git bundle create $bundlePath $Branch "^$previous"
    git bundle verify $bundlePath
    & scp -i $KeyPath $bundlePath "${remote}:$remoteBundle"
    if ($LASTEXITCODE -ne 0) {
        throw "Bundle upload failed."
    }

    $remoteCommand = @"
set -Eeuo pipefail
cd '$remoteRepo'
git fetch '$remoteBundle' 'refs/heads/$Branch:refs/remotes/upload/$Branch'
git merge --ff-only 'refs/remotes/upload/$Branch'
rm -f '$remoteBundle'
SKIP_GIT_FETCH=1 ./deploy/server/update.sh
"@
    & ssh -i $KeyPath $remote $remoteCommand
    if ($LASTEXITCODE -ne 0) {
        throw "Remote deployment failed."
    }
}
finally {
    Remove-Item -LiteralPath $bundlePath -Force -ErrorAction SilentlyContinue
}
