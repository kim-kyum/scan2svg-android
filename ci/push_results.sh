#!/bin/bash
# 빌드·테스트 결과를 별도 브랜치에 올린다(Actions 화면을 열지 않고도 결과 파일을 받아 볼 수 있게).
# 사용: push_results.sh <브랜치> <폴더>
set -e
BRANCH=$1
DIR=$2
mkdir -p "$DIR"
cd "$DIR"
find . -type f -size +90M -delete   # 깃허브는 100MB 넘는 파일을 받지 않는다
git init -q
git checkout -q -b "$BRANCH"
git add -A
git -c user.name="github-actions" -c user.email="github-actions@users.noreply.github.com" \
    commit -q --allow-empty -m "results for $GITHUB_SHA (run $GITHUB_RUN_NUMBER)"
git push -q -f "https://x-access-token:${GITHUB_TOKEN}@github.com/${GITHUB_REPOSITORY}.git" "$BRANCH"
