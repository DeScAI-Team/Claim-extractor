import {upload, retrieve} from './arweaveService.js';
import fs from 'fs';

//Command Line Arguments From User
const testFile = process.argv[2];
const outputFile = process.argv[3];

//If Command Line Arguments Not Written
if(!testFile){
  console.error('Error: Please provide a test file (and download path if desired) as argument.');
  process.exit(1);
}

async function runTest(){
  console.log('--- Starting Upload ---');
  const uploadInfo = await upload(testFile);
  
  if (uploadInfo.success) {
    console.log(`File is live at: ${uploadInfo.webUrl}`);
    
    //Write (if argument given)
    if(outputFile){
      console.log('\n--- Starting Retrieval ---');
      const download = await retrieve(uploadInfo.txId);

      if (download.success && outputFile !== null){
        fs.writeFile(outputFile, Buffer.from(download.data), (err) => {
          if (err) {
            console.error('Error writing file:', err);
          } else {
            console.log('File written successfully to:', outputFile);
          }
        });
      }
    }
  }
}

runTest();