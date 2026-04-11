import dotenv from 'dotenv';
import {TurboFactory, ArweaveSigner} from '@ardrive/turbo-sdk';
import fs from 'fs';

dotenv.config();

//Access wallet
const WALLET_PATH = process.env.WALLET_PATH;

if (!WALLET_PATH) {
  throw new Error("Missing WALLET_PATH in .env");
}

// Load Arweave Wallet
const jwk = JSON.parse(fs.readFileSync(WALLET_PATH, 'utf-8'));
const signer = new ArweaveSigner(jwk);
const turbo = TurboFactory.authenticated({signer})

/**
 * Uploads file and returns transaction ID and Web URL
 * @param {string} filePath - Path to File to be uplaoded
 */
export async function upload(filePath){
    try {
    fs.accessSync(filePath, fs.constants.R_OK);
    if(!fs.statSync(filePath).isFile()){
        throw new Error('Path is not a file!');
    }
    }catch(err){
        throw new Error('Invalid file Path!');
    }
    try{
        const fileSize =  fs.statSync(filePath).size;

        const uploadResult =  await turbo.uploadFile({
            fileStreamFactory: () => fs.createReadStream(filePath),
            fileSizeFactory: () => fileSize,
        });
    
        const txId = uploadResult.id;
        const webUrl = `https://arweave.net/${txId}`;

        return{
            success: true,
            txId,
            webUrl
        };
    }catch(error){
        console.error('Upload failed: ', error.message);
        return {success: false, error: error.message};
    }
}

/**
 * Uploads file and returns transaction ID and web URL
 * @param {string} txId - Arweave Transcation ID
 */
export async function retrieve(txId){
    try{
        const gatewayUrl = `https://arweave.net/${txId}`;
    
        const response = await fetch(gatewayUrl);
    
        if (!response.ok){
        throw new Error(`Failed to fetch: ${response.statusText}`);
        }

        const data = await response.arrayBuffer();

        return {
      success: true,
      data: data,
    };
    }catch(error){
        console.error('Retrieval failed:', error.message);
        return{sucess: false, error: error.message};
    }
}