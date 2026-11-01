import unittest
import asyncio
from sandbox.validator import validator
from sandbox.executor import executor
from profiles.rbac import rbac
from audit.trail import audit

class TestSandbox(unittest.IsolatedAsyncioTestCase):
    async def test_sandbox(self):
        print("--- Running Sandbox Tests ---")
        # 1. Test Command Risk Scorer
        print("\n1. Testing Risk Scorer...")
        safe_cmd = "dir"
        high_cmd = "del target.txt"
        crit_cmd = "rm -rf /"
        print(f"[{safe_cmd}] -> {validator.score_command(safe_cmd)}")
        print(f"[{high_cmd}] -> {validator.score_command(high_cmd)}")
        print(f"[{crit_cmd}] -> {validator.score_command(crit_cmd)}")
        # 2. Test Execution Sandbox
        print("\n2. Testing Execution (Safe Command)...")
        result = await executor.execute("echo Hello Primus", "CodingAgent")
        print(f"Result: {result['stdout'].strip()}")
        print("\n3. Testing Execution (Blocked Command)...")
        result = await executor.execute("Remove-Item -Recurse -Force C:/Windows", "CodingAgent")
        print(f"Blocked: {result['blocked']} | Reason: {result['stderr']}")
        # 4. Test RBAC
        print("\n4. Testing RBAC...")
        try:
            rbac.validate_permission("BrowserAgent", "write_workspace")
        except Exception as e:
            print(f"RBAC caught unauthorized access: {e}")
        print("\n--- Tests Complete ---")

if __name__ == "__main__":
    unittest.main()
