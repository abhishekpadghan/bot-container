"""Re-run the lab with a deeper in-the-money option (delta 0.7) to test the cost-drag theory."""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import strategy_lab as lab
lab.DELTA = 0.7
print("### DELTA = 0.7 (deeper ITM; theta left unchanged = pessimistic)")
lab.main()
