#!/usr/bin/env python3
"""Actual native file model and Kit retry; host substitutes only, no device or network."""
import argparse, hashlib, json, subprocess, tempfile
from pathlib import Path
from native_home_retry_check import SUPPORT

CHECKS = r'''

@main struct FileReview {
 @MainActor static func main() async throws {
  var results:[[String:Any]]=[]
  func run(_ name:String,_ test:() async throws -> Void) async {
   Keychain.reset(); AppAttestManager.signatures=[]
   do {try await test();results.append(["case":name,"passed":true])}
   catch {results.append(["case":name,"passed":false,"error":String(describing:error)])}
  }
  let folder=FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
  try FileManager.default.createDirectory(at:folder,withIntermediateDirectories:true)
  defer {try? FileManager.default.removeItem(at:folder)}
  let url=folder.appendingPathComponent("Tabelle.csv")
  try Data("a,b\n1,2\n".utf8).write(to:url)
  await run("fresh_view_missing_files_cannot_replace_uncertain_file_request") {
   let c=ApprovalClient(),first=TaskStartModel()
   first.objective="Analysiere die angehängte Tabelle."
   await first.selectFiles([url]);c.submit={_ in throw URLError(.networkConnectionLost)}
   await first.send(client:c,keyID:keyID)
   try check(c.submitted.count==1 && first.hasUnconfirmedRetry,"first missing")
   let original=c.submitted[0],fresh=TaskStartModel()
   fresh.restoreRetryHint(client:c);fresh.objective=first.objective;c.submit=nil
   await fresh.send(client:c,keyID:keyID)
   try check(c.submitted==[original],"A fresh view silently submitted a new request without the original files")
  }
  await run("fresh_view_same_files_reuses_exact_request_with_fresh_proof") {
   let c=ApprovalClient(),first=TaskStartModel();first.objective="Analysiere die angehängte Tabelle."
   await first.selectFiles([url]);c.submit={_ in throw URLError(.networkConnectionLost)}
   await first.send(client:c,keyID:keyID);let original=c.submitted[0]
   let fresh=TaskStartModel();fresh.restoreRetryHint(client:c);fresh.objective=first.objective
   await fresh.selectFiles([url]);c.submit=nil;await fresh.send(client:c,keyID:keyID)
   try check(c.submitted==[original,original] && fresh.accepted != nil,"same retry changed")
   try check(AppAttestManager.signatures.count==2 && AppAttestManager.signatures[0] != AppAttestManager.signatures[1],"proof reused")
  }
  await run("cancelled_file_read_releases_loading_state") {
   let big=folder.appendingPathComponent("Big.csv");try Data(repeating:65,count:8*1024*1024).write(to:big)
   let model=TaskStartModel();model.objective="Analysiere diese Tabelle vollständig."
   let reader=Task{await model.selectFiles([big])}
   for _ in 0..<1000 {if model.documentLoading {break};await Task.yield()}
   try check(model.documentLoading,"read not entered");reader.cancel();await reader.value
   try check(!model.documentLoading && model.files == nil,"cancelled read leaves permanent loading state")
  }

  await run("fresh_view_scope_change_and_legacy_metadata_cannot_replace_unknown_request") {
   let c=ApprovalClient(),first=TaskStartModel();first.objective="Analysiere die angehängte Tabelle."
   await first.selectFiles([url]);c.submit={_ in throw URLError(.networkConnectionLost)}
   await first.send(client:c,keyID:keyID);let original=c.submitted[0]
   let cacheKey=Keychain.state.values.keys.first!
   var metadata=try JSONSerialization.jsonObject(with:Keychain.load(tag:cacheKey)!) as! [String:Any]
   metadata.removeValue(forKey:"attachmentKind")
   Keychain.save(try JSONSerialization.data(withJSONObject:metadata),tag:cacheKey)
   let fresh=TaskStartModel();fresh.restoreRetryHint(client:c)
   fresh.scope="build";fresh.objective=first.objective;c.submit=nil
   await fresh.send(client:c,keyID:keyID)
   try check(c.submitted==[original] && fresh.hasUnconfirmedRetry,"legacy metadata/scope erased uncertainty")
  }
  await run("explicit_new_request_can_leave_previous_unknown_unchanged") {
   let c=ApprovalClient(),first=TaskStartModel();first.objective="Analysiere die angehängte Tabelle."
   await first.selectFiles([url]);c.submit={_ in throw URLError(.networkConnectionLost)}
   await first.send(client:c,keyID:keyID);let original=c.submitted[0]
   let fresh=TaskStartModel();fresh.restoreRetryHint(client:c);fresh.reset()
   fresh.objective="Ein ausdrücklich neu begonnener Auftrag.";c.submit=nil
   await fresh.send(client:c,keyID:keyID)
   try check(c.submitted.count==2 && c.submitted[1].client_request_id != original.client_request_id,"explicit new task not distinct")
   try check(fresh.accepted != nil,"explicit new task refused")
  }
  let failed=results.filter{$0["passed"] as? Bool==false}.count
  print(String(decoding:try JSONSerialization.data(withJSONObject:["cases":results,"passed":results.count-failed,"failed":failed],options:[.sortedKeys]),as:UTF8.self))
 }
}
'''

def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--kit-build',type=Path,required=True)
    args=parser.parse_args()
    root=Path(__file__).resolve().parents[1]
    source=root/'App/TaskStart.swift';raw=source.read_bytes();text=raw.decode()
    model=text[text.index('private enum TaskRetryCache {'):text.index('struct TaskStartView: View {')]
    build=args.kit_build/'arm64-apple-macosx/debug'
    objects=sorted((build/'SolvioApprovalsKit.build').glob('*.swift.o'))
    if not objects:raise SystemExit('Missing existing Kit objects; run swift test first.')
    with tempfile.TemporaryDirectory(prefix='solvio-native-file-retry-') as temporary:
        folder=Path(temporary);harness=folder/'Check.swift';binary=folder/'check'
        harness.write_text(SUPPORT+'\n'+model+'\n'+CHECKS)
        subprocess.run(['xcrun','swiftc','-swift-version','6','-parse-as-library','-I',str(build/'Modules'),str(harness),*map(str,objects),'-o',str(binary)],check=True)
        result=subprocess.run([str(binary)],check=True,capture_output=True,text=True,timeout=30)
        if source.read_bytes()!=raw:raise SystemExit('Model changed during probe.')
        proof=json.loads(result.stdout)
        proof.update(source=str(source),source_sha256=hashlib.sha256(raw).hexdigest(),
            network_calls=0,device_calls=0,keychain='temporary_dictionary',signer='synthetic',
            method='verbatim_native_model_and_cache_with_host_substitutes')
        print(json.dumps(proof,ensure_ascii=False,indent=2))
        raise SystemExit(1 if proof['failed'] else 0)
if __name__=='__main__':main()
