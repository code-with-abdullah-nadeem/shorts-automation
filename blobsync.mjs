// Moves pipeline state and videos between a GitHub Actions runner and the private Vercel Blob store.
// Needs BLOB_READ_WRITE_TOKEN. All paths in the store live under "data/".
//   node blobsync.mjs pull <remote> <local>      (missing remote file = no-op, exit 0)
//   node blobsync.mjs push <local> <remote>      (skipped when unchanged since pull/last push)
//   node blobsync.mjs del  <remote>
//   node blobsync.mjs has  <remote>              (prints yes/no)
import { get, put, del, head } from "@vercel/blob";
import { createHash } from "node:crypto";
import { mkdirSync, readFileSync, writeFileSync, existsSync, createReadStream, statSync } from "node:fs";
import { dirname } from "node:path";
import { Readable } from "node:stream";
import { finished } from "node:stream/promises";
import { createWriteStream } from "node:fs";

const [cmd, a, b] = process.argv.slice(2);
const key = (p) => `data/${p.replace(/^\/+/, "")}`;
const sha = (f) => createHash("sha1").update(readFileSync(f)).digest("hex");
const marker = (f) => `${f}.blobsha`;

async function main() {
  if (cmd === "pull") {
    let r;
    try {
      r = await get(key(a), { access: "private", useCache: false });
    } catch (e) {
      if (e?.name === "BlobNotFoundError") return;
      throw e;
    }
    if (!r || r.statusCode !== 200) return;
    mkdirSync(dirname(b), { recursive: true });
    await finished(Readable.fromWeb(r.stream).pipe(createWriteStream(b)));
    writeFileSync(marker(b), sha(b));
  } else if (cmd === "push") {
    if (!existsSync(a)) return;
    const h = sha(a);
    if (existsSync(marker(a)) && readFileSync(marker(a), "utf8") === h) return; // unchanged: no write
    const big = statSync(a).size > 50 * 1024 * 1024;
    await put(key(b), big ? createReadStream(a) : readFileSync(a), {
      access: "private", allowOverwrite: true, addRandomSuffix: false, multipart: big, cacheControlMaxAge: 60,
    });
    writeFileSync(marker(a), h);
    console.log(`stored ${b}`);
  } else if (cmd === "del") {
    try { await del(key(a)); console.log(`deleted ${a}`); } catch (e) { if (e?.name !== "BlobNotFoundError") throw e; }
  } else if (cmd === "has") {
    try { await head(key(a)); console.log("yes"); } catch { console.log("no"); }
  } else {
    console.error("usage: pull|push|del|has");
    process.exit(2);
  }
}
main().catch((e) => { console.error(e?.message || e); process.exit(1); });
