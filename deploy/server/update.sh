#!/usr/bin/env bash
set -Eeuo pipefail

branch="${DEPLOY_BRANCH:-agent/frontend-apple-refactor}"
repo_root="$(git rev-parse --show-toplevel)"
compose_file="${repo_root}/deploy/server/docker-compose.yml"
env_file="${repo_root}/deploy/server/.env"
backend_image="multiagent-decision-backend:online"
frontend_image="multiagent-decision-frontend:online"
backend_rollback="multiagent-decision-backend:online-rollback"
frontend_rollback="multiagent-decision-frontend:online-rollback"

cd "${repo_root}"

if [[ ! -f "${env_file}" ]]; then
  echo "missing ${env_file}; copy .env.example and provide secrets first" >&2
  exit 1
fi
if ! docker image inspect multiagent-decision-database:1.1.0 >/dev/null 2>&1; then
  echo "missing database image multiagent-decision-database:1.1.0" >&2
  exit 1
fi
if [[ -n "$(git status --porcelain --untracked-files=no)" ]]; then
  echo "tracked files contain local changes; refusing to overwrite them" >&2
  exit 1
fi

previous_commit="$(git rev-parse HEAD)"
git fetch origin "${branch}"
git checkout "${branch}"
git merge --ff-only "origin/${branch}"
target_commit="$(git rev-parse HEAD)"

if docker image inspect "${backend_image}" >/dev/null 2>&1; then
  docker tag "${backend_image}" "${backend_rollback}"
fi
if docker image inspect "${frontend_image}" >/dev/null 2>&1; then
  docker tag "${frontend_image}" "${frontend_rollback}"
fi

rollback() {
  rc=$?
  echo "deployment failed; restoring previous images and commit ${previous_commit}" >&2
  if docker image inspect "${backend_rollback}" >/dev/null 2>&1; then
    docker tag "${backend_rollback}" "${backend_image}"
  fi
  if docker image inspect "${frontend_rollback}" >/dev/null 2>&1; then
    docker tag "${frontend_rollback}" "${frontend_image}"
  fi
  git checkout --detach "${previous_commit}" >/dev/null 2>&1 || true
  docker compose --env-file "${env_file}" --file "${compose_file}" up --detach --wait --wait-timeout 180 || true
  exit "${rc}"
}
trap rollback ERR

docker compose --env-file "${env_file}" --file "${compose_file}" build backend frontend
docker compose --env-file "${env_file}" --file "${compose_file}" up --detach --wait --wait-timeout 240

trap - ERR
printf '%s\n' "${target_commit}" > "${repo_root}/deploy/server/.deployed-commit"
docker image prune --force --filter "until=168h" >/dev/null

echo "deployed ${target_commit} from ${branch}"
docker compose --env-file "${env_file}" --file "${compose_file}" ps
