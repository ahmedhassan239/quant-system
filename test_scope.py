def test():
    if True:
        try:
            print(order)
        except Exception as e:
            print(f"Error: {repr(e)}")
    order = None
    try:
        pass
    except:
        pass
test()
