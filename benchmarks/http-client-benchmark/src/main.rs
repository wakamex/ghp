use std::collections::HashMap;
use std::env;
use std::process::Command;
use std::time::Instant;

use reqwest::header::{ACCEPT, AUTHORIZATION, HeaderMap, HeaderValue, USER_AGENT};
use serde_json::{Value, json};

const API_BASE: &str = "https://api.github.com";

fn github_token() -> Option<String> {
    for name in ["GITHUB_PAT", "GITHUB_TOKEN", "GH_TOKEN"] {
        if let Ok(token) = env::var(name)
            && !token.is_empty()
        {
            return Some(token);
        }
    }

    let output = Command::new("gh").args(["auth", "token"]).output().ok()?;
    if !output.status.success() {
        return None;
    }
    let token = String::from_utf8(output.stdout).ok()?.trim().to_owned();
    (!token.is_empty()).then_some(token)
}

fn client() -> Result<reqwest::Client, Box<dyn std::error::Error>> {
    let mut headers = HeaderMap::new();
    headers.insert(
        USER_AGENT,
        HeaderValue::from_static("ghp-rust-prototype/0.1"),
    );
    headers.insert(
        ACCEPT,
        HeaderValue::from_static("application/vnd.github+json"),
    );
    headers.insert(
        "x-github-api-version",
        HeaderValue::from_static("2022-11-28"),
    );
    if let Some(token) = github_token() {
        headers.insert(
            AUTHORIZATION,
            HeaderValue::from_str(&format!("Bearer {token}"))?,
        );
    }

    Ok(reqwest::Client::builder()
        .default_headers(headers)
        .build()?)
}

async fn fetch(
    client: &reqwest::Client,
    path: &str,
    params: &[(&str, &str)],
) -> Result<Vec<Value>, reqwest::Error> {
    client
        .get(format!("{API_BASE}/{path}"))
        .query(params)
        .send()
        .await?
        .error_for_status()?
        .json()
        .await
}

#[tokio::main]
async fn main() -> Result<(), Box<dyn std::error::Error>> {
    let mut args = env::args().skip(1);
    let repo = args
        .next()
        .ok_or("usage: ghp-rust-prototype OWNER/REPO SINCE")?;
    let since = args
        .next()
        .ok_or("usage: ghp-rust-prototype OWNER/REPO SINCE")?;
    if args.next().is_some() || repo.split_once('/').is_none() {
        return Err("usage: ghp-rust-prototype OWNER/REPO SINCE".into());
    }

    let client = client()?;
    let started = Instant::now();

    let issues_path = format!("repos/{repo}/issues");
    let prs_path = format!("repos/{repo}/pulls");
    let issue_comments_path = format!("repos/{repo}/issues/comments");
    let review_comments_path = format!("repos/{repo}/pulls/comments");
    let commits_path = format!("repos/{repo}/commits");

    let issues_params = [
        ("state", "all"),
        ("sort", "updated"),
        ("direction", "desc"),
        ("since", since.as_str()),
        ("per_page", "60"),
        ("page", "1"),
    ];
    let prs_params = [
        ("state", "all"),
        ("sort", "updated"),
        ("direction", "desc"),
        ("per_page", "30"),
        ("page", "1"),
    ];
    let comment_params = [
        ("since", since.as_str()),
        ("sort", "updated"),
        ("direction", "desc"),
        ("per_page", "30"),
        ("page", "1"),
    ];
    let commit_params = [("since", since.as_str()), ("per_page", "30"), ("page", "1")];

    let (issues, prs, issue_comments, review_comments, commits) = tokio::try_join!(
        fetch(&client, &issues_path, &issues_params),
        fetch(&client, &prs_path, &prs_params),
        fetch(&client, &issue_comments_path, &comment_params),
        fetch(&client, &review_comments_path, &comment_params),
        fetch(&client, &commits_path, &commit_params),
    )?;

    let issue_count = issues
        .iter()
        .filter(|item| item.get("pull_request").is_none())
        .count()
        .min(30);
    let pr_count = prs
        .iter()
        .filter(|item| {
            item.get("updated_at")
                .and_then(Value::as_str)
                .is_some_and(|updated| updated >= since.as_str())
        })
        .count()
        .min(30);

    let counts = HashMap::from([
        ("issues", issue_count),
        ("pull_requests", pr_count),
        (
            "recent_comments",
            (issue_comments.len() + review_comments.len()).min(30),
        ),
        ("commits", commits.len().min(30)),
    ]);
    println!(
        "{}",
        json!({
            "counts": counts,
            "fetch_ms": started.elapsed().as_secs_f64() * 1000.0,
        })
    );
    Ok(())
}
