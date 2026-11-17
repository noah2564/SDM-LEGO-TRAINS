import sys
def step(n, fn):
    print("step", n, "...")
    try:
        r = fn()
        print("step", n, "ok")
        return r
    except BaseException as e:
        print("step", n, "FAILED:")
        sys.print_exception(e)
        raise

main = step(1, lambda: __import__("main"))
step(2, lambda: main.StatusLed(True))
step(3, lambda: __import__("bluetooth").BLE().active(True))
step(4, lambda: main.Lwp3Hub(print))
step(5, lambda: main.ObstacleGuard())
node = step(6, lambda: main.TrainNode())
print("node built, running (Ctrl+C to stop)")
node.run()
