import {defineConfig} from 'vite';
import react from '@vitejs/plugin-react';
import tailwind from '@tailwindcss/vite';
import path from 'node:path';
import fs from 'node:fs';
import crypto from 'node:crypto';
import {createRequire} from 'node:module';
const app=path.resolve(__dirname,'..'),repo=path.resolve(app,'../..');
const requireFromApp=createRequire(path.join(app,'package.json'));
const hash=(filename:string)=>crypto.createHash('sha256').update(fs.readFileSync(filename)).digest('hex');
export default defineConfig({root:__dirname,base:'/dashboard/hermes/',publicDir:false,
  plugins:[react(),tailwind(),{name:'solvio-readonly-build-proof',enforce:'pre',
  transform(code,id){
    // These two display leaves also offer optional native-host conveniences.
    // The external reader has no host bridge: remove that capability at build
    // time, including a future host injection. Original renderer sources stay
    // pinned; the exact transformation is part of the Core adapter hash.
    if(id.endsWith('/src/i18n/context.tsx')||id.endsWith('/src/lib/external-link.tsx'))
      return {code:code.replaceAll('window.hermesDesktop','(undefined as never)'),map:null};
  },generateBundle(_options,bundle){
    const sources:Record<string,string>={},packages:Record<string,any>={};
    for(const item of Object.values(bundle))if(item.type==='chunk')for(const filename of Object.keys(item.modules)){
      const normalized=filename.replaceAll('\\','/');
      if(/\/apps\/desktop\/src\/(api\/|hermes\.ts|store\/gateway\.ts|app\/session\/)/.test(normalized)||normalized.includes('/json-rpc-gateway.'))throw Error('Execution transport entered renderer: '+filename);
      if(filename.startsWith(repo+'/')&&!filename.includes('/node_modules/')&&fs.existsSync(filename))sources[path.relative(repo,filename)]=hash(filename);
      if(filename.includes('/node_modules/')&&fs.existsSync(filename)){
        let folder=path.dirname(filename);
        while(folder.startsWith(repo+'/')){
          const candidate=path.join(folder,'package.json');
          if(fs.existsSync(candidate)&&JSON.parse(fs.readFileSync(candidate,'utf8')).name)break;
          folder=path.dirname(folder);
        }
        const descriptor=path.join(folder,'package.json');
        if(folder.startsWith(repo+'/')&&fs.existsSync(descriptor)){
          const info=JSON.parse(fs.readFileSync(descriptor,'utf8'));
          const licenses=fs.readdirSync(folder).filter(name=>/^(license|licence|copying|notice)(\.|$|-)/i.test(name)&&fs.statSync(path.join(folder,name)).isFile());
          packages[path.relative(repo,folder)]={name:info.name,version:info.version,license:info.license,package_sha256:hash(descriptor),licenses};
        }
      }
    }
    for(const item of Object.values(bundle))if(item.type==='chunk'&&item.code.includes('hermesDesktop'))throw Error('Native host bridge entered renderer');
    this.emitFile({type:'asset',fileName:'SOURCE.json',source:JSON.stringify({sources,packages,execution_transport_modules:0,
      host_bridge:'disabled_at_build',transformed_display_leaves:['src/i18n/context.tsx','src/lib/external-link.tsx']},null,2)});
  }}],css:{postcss:{plugins:[]}},resolve:{alias:[
    {find:'@/hermes',replacement:path.join(__dirname,'config-closed.ts')},
    {find:'@/store/preview',replacement:path.join(__dirname,'preview-closed.ts')},
    {find:'@',replacement:path.join(app,'src')},
    {find:'react',replacement:path.dirname(requireFromApp.resolve('react/package.json'))},
    {find:'react-dom',replacement:path.dirname(requireFromApp.resolve('react-dom/package.json'))}
  ]},build:{outDir:path.join(repo,'solvio-desktop-view-dist'),emptyOutDir:true,
    rollupOptions:{input:path.join(__dirname,'solvio-view.html')}}});
