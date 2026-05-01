const fs = require("fs");
const path = require("path");

const repoRoot = path.resolve(__dirname, "..", "..", "..");
const source = path.join(repoRoot, "contract", "oauth_contract.json");
const targets = [
  path.join(__dirname, "..", ".next", "contract", "oauth_contract.json"),
  path.join(__dirname, "..", ".next", "standalone", ".next", "contract", "oauth_contract.json")
];

if (!fs.existsSync(source)) {
  console.error(`contract file not found: ${source}`);
  process.exit(1);
}

for (const target of targets) {
  fs.mkdirSync(path.dirname(target), { recursive: true });
  fs.copyFileSync(source, target);
}
