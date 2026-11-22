interface Env {
  GITHUB_TOKEN: string;
  GITHUB_REPO: string;
  GITHUB_WORKFLOW: string;
  GITHUB_REF: string;
  NTFY_TOPIC: string;
}

async function dispatch(env: Env): Promise<void> {
  const url = `https://api.github.com/repos/${env.GITHUB_REPO}/actions/workflows/${env.GITHUB_WORKFLOW}/dispatches`;
  const resp = await fetch(url, {
    method: "POST",
    headers: {
      Accept: "application/vnd.github+json",
      Authorization: `Bearer ${env.GITHUB_TOKEN}`,
      "X-GitHub-Api-Version": "2022-11-28",
      "User-Agent": "apps-ping-dispatcher",
      "Content-Type": "application/json",
    },
    body: JSON.stringify({ ref: env.GITHUB_REF }),
  });
  if (!resp.ok) {
    const body = await resp.text();
    throw new Error(`GitHub returned ${resp.status}: ${body.slice(0, 300)}`);
  }
}

async function notify(env: Env, message: string): Promise<void> {
  try {
    await fetch(`https://ntfy.sh/${env.NTFY_TOPIC}`, {
      method: "POST",
      headers: {
        Title: "APPS ping dispatcher failed",
        Tags: "satellite,warning",
        Priority: "high",
        Click: `https://github.com/${env.GITHUB_REPO}/actions`,
      },
      body: message,
    });
  } catch (err) {
    console.error("ntfy notification failed", err);
  }
}

export default {
  async scheduled(_controller, env, _ctx): Promise<void> {
    try {
      await dispatch(env);
      console.log(`Dispatched ${env.GITHUB_WORKFLOW} on ${env.GITHUB_REPO}`);
    } catch (err) {
      const message = `Could not start the apps-ping workflow, so APPS is not being checked. ${err}`;
      console.error(message);
      await notify(env, message);
      throw err; // mark the invocation as failed in the Workers logs
    }
  },
} satisfies ExportedHandler<Env>;
