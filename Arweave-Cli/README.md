# Arweave Service

A Node.js module for uploading and retrieving files on the [Arweave](https://www.arweave.org/) permaweb using the [ArDrive Turbo SDK](https://github.com/ardriveapp/turbo-sdk).

## Prerequisites

- Node.js (v18+)
- Dependencies installed (`npm install` in this folder)
- An **Arweave wallet keyfile**: a single JSON file (JWK) that holds the key material used to sign uploads. You do **not** put a separate “address” and “private key” in `.env` — you point `WALLET_PATH` at that JSON file. Create or export a wallet from [Arweave](https://www.arweave.org/) / ArConnect and keep the file secret; never commit it.
- **Credits**: Turbo uploads need a funded wallet (AR and/or Turbo credits per [Turbo](https://docs.ardrive.io/docs/turbo/) / ArDrive docs).

## Setup

1. From this directory, run:

```bash
npm install
```

2. Copy `.env.example` to `.env` and set `WALLET_PATH` to the **absolute or relative path** of your JWK file (relative paths are resolved from this directory when you run Node here).

```env
WALLET_PATH=path/to/your/arweave-keyfile.json
```

If `WALLET_PATH` is missing or `npm install` was skipped, uploads will fail before any data hits Arweave.

## Command Line Usage

You can use the CLI to upload a file to Arweave and optionally retrieve it immediately.

### Upload a File

```bash
node arweaveServiceCLI.js <path/to/file>
```

- Uploads the file and prints the Arweave transaction ID and web URL.

### Upload and Retrieve a File

```bash
node arweaveServiceCLI.js <path/to/file> <output/path>
```

- Uploads the file, then downloads it from Arweave and saves it to `<output/path>`.

**Example:**

```bash
node arweaveServiceCLI.js ./example.txt ./downloaded.txt
```

### Python orchestrator (receipt JSON)

```bash
python upload_orchestrator.py -f /path/to/file.json
```

Runs the same CLI with `cwd` set to this folder, then writes `upload_reciept.json` (stdout, stderr, parsed `tx_id` / `web_url` when the upload line is present).

This uploads `example.txt` and writes the downloaded file to `downloaded.txt` after upload.

**Note:**  
Make sure your `.env` file is configured with `WALLET_PATH` pointing to your Arweave wallet key file.

## API Usage 

### Import the Module

```js
import { upload, retrieve } from './arweaveService.js';
// or, if using CommonJS:
// const { upload, retrieve } = require('./arweaveService.js');
```

### Upload a File

```js
const result = await upload('path/to/your/file.txt');
if (result.success) {
  console.log('Transaction ID:', result.txId);
  console.log('Web URL:', result.webUrl);
} else {
  console.error('Upload failed:', result.error);
}
```

**Returns:**

| Field   | Type    | Description                                 |
|---------|---------|---------------------------------------------|
| success | boolean | Whether the upload succeeded                |
| txId    | string  | Arweave transaction ID                      |
| webUrl  | string  | Public URL (`https://arweave.net/<txId>`)   |
| error   | string  | Error message (on failure)                  |

### Retrieve a File

```js
const result = await retrieve('your-arweave-txid');
if (result.success) {
  // result.data is an ArrayBuffer
  // Convert to Buffer if needed: Buffer.from(result.data)
  console.log('File retrieved successfully');
} else {
  console.error('Retrieval failed:', result.error);
}
```

**Returns:**

| Field   | Type        | Description                        |
|---------|-------------|------------------------------------|
| success | boolean     | Whether the retrieval succeeded    |
| data    | ArrayBuffer | The raw file data                  |
| error   | string      | Error message (on failure)         |

## API Reference

| Function           | Parameters           | Description                              |
|--------------------|---------------------|------------------------------------------|
| `upload(filePath)` | `filePath` (string) | Uploads a file to Arweave via Turbo SDK. Returns a Promise. |
| `retrieve(txId)`   | `txId` (string)     | Fetches file data from the Arweave gateway. Returns a Promise. |

## Error Handling

Both functions return an object with `success: false` and an `error` message if something goes wrong (e.g., missing wallet, network error, invalid txId).

## License

MIT
