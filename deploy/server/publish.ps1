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
$remoteImages = "/opt/multiagent/update-images.tar"
$previous = (& ssh -i $KeyPath $remote "cd '$remoteRepo' && git rev-parse HEAD").Trim()

git merge-base --is-ancestor $previous $target
if ($LASTEXITCODE -ne 0) {
    throw "Server commit $previous is not an ancestor of $target; refusing a non-fast-forward publish."
}

$bundlePath = Join-Path ([System.IO.Path]::GetTempPath()) "multiagent-update-$target.bundle"
$imagesPath = Join-Path ([System.IO.Path]::GetTempPath()) "multiagent-images-$target.tar"
try {
    git bundle create $bundlePath $Branch "^$previous"
    git bundle verify $bundlePath

    docker build --file docker/backend/Dockerfile --tag multiagent-decision-backend:online .
    if ($LASTEXITCODE -ne 0) {
        throw "Local backend image build failed."
    }
    docker build --file docker/frontend/Dockerfile --tag multiagent-decision-frontend:online .
    if ($LASTEXITCODE -ne 0) {
        throw "Local frontend image build failed."
    }
    docker save --output $imagesPath multiagent-decision-backend:online multiagent-decision-frontend:online
    if ($LASTEXITCODE -ne 0) {
        throw "Local image export failed."
    }

    & scp -C -i $KeyPath $bundlePath "${remote}:$remoteBundle"
    if ($LASTEXITCODE -ne 0) {
        throw "Bundle upload failed."
    }
    & scp -C -i $KeyPath $imagesPath "${remote}:$remoteImages"
    if ($LASTEXITCODE -ne 0) {
        throw "Image upload failed."
    }

    $remoteCommand = @"
set -Eeuo pipefail
cd '$remoteRepo'
if docker image inspect multiagent-decision-backend:online >/dev/null 2>&1; then
  docker tag multiagent-decision-backend:online multiagent-decision-backend:online-rollback
fi
if docker image inspect multiagent-decision-frontend:online >/dev/null 2>&1; then
  docker tag multiagent-decision-frontend:online multiagent-decision-frontend:online-rollback
fi
docker load --input '$remoteImages'
git fetch '$remoteBundle' 'refs/heads/${Branch}:refs/remotes/upload/${Branch}'
git merge --ff-only 'refs/remotes/upload/${Branch}'
rm -f '$remoteBundle' '$remoteImages'
DEPLOY_PREVIOUS_COMMIT='$previous' SKIP_GIT_FETCH=1 SKIP_BUILD=1 SKIP_IMAGE_BACKUP=1 ./deploy/server/update.sh
"@
    & ssh -i $KeyPath $remote $remoteCommand
    if ($LASTEXITCODE -ne 0) {
        throw "Remote deployment failed."
    }
}
finally {
    Remove-Item -LiteralPath $bundlePath -Force -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath $imagesPath -Force -ErrorAction SilentlyContinue
}
