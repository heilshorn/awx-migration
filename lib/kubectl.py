import json,subprocess,shutil
from .config import NAMESPACE
class KubectlError(RuntimeError): pass
class Kubectl:
  def __init__(self,namespace=NAMESPACE): self.namespace=namespace
  def run(self,args):
    if shutil.which("kubectl") is None: raise KubectlError("kubectl not found")
    p=subprocess.run(["kubectl","-n",self.namespace,*args],capture_output=True,text=True)
    if p.returncode: raise KubectlError(p.stderr.strip())
    return p.stdout.strip()
  def list_pods(self): return json.loads(self.run(["get","pods","-o","json"]))["items"]
