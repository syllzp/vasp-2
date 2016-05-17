"""The core Vasp calculator.

I aim to keep this file at a minimum. Hence, many logically grouped
class methods are actually imported at the end.

"""

import os
import subprocess
import warnings
import numpy as np
import ase
from ase.calculators.calculator import Calculator
from ase.calculators.calculator import FileIOCalculator
from ase.io.vasp import read_vasp_xml

# internal modules
import exceptions
from vasp import log
import validate
from vasprc import VASPRC


def VaspExceptionHandler(calc, exc_type, exc_value, exc_traceback):
    """Handle exceptions."""
    if exc_type == exceptions.VaspSubmitted:
        print exc_value
        return None
    elif exc_type == exceptions.VaspQueued:
        print exc_value
        return None
    elif exc_type == KeyError and exc_value.message == 'energy':
        return None
    elif exc_type == KeyError and exc_value.message == 'forces':
        return np.array([[None, None, None] for atom in calc.get_atoms()])
    elif exc_type == KeyError and exc_value.message == 'stress':
        return np.array([None, None, None, None, None, None])

    print('Unhandled exception in Vasp')
    import traceback
    import sys
    traceback.print_exception(exc_type, exc_value, exc_traceback,
                              file=sys.stdout)
    raise


class Vasp(FileIOCalculator, object):
    """Class for doing VASP calculations.

    Configurations are in vasp.vasprc

    POTCARs are found in:
    $VASP_PP_PATH/potpaw_LDA
    $VASP_PP_PATH/potpaw_PBE
    $VASP_PP_PATH/potpaw_GGA

    """

    name = 'VASP'
    command = None
    debug = None

    implemented_properties = ['energy', 'forces', 'stress',
                              'charges', 'dipole',
                              'magmom',  # the overall magnetic moment
                              'magmoms']  # the individual magnetic moments

    # These allow you to use simple strings for the xc kwarg and automatically
    # set the relevant vasp tags.
    xc_defaults = {'lda': {'pp': 'LDA'},
                   # GGAs
                   'pbe': {'pp': 'PBE'},
                   'revpbe': {'pp': 'LDA', 'gga': 'RE'},
                   'rpbe': {'pp': 'LDA', 'gga': 'RP'},
                   'am05': {'pp': 'LDA', 'gga': 'AM'},
                   'pbesol': {'pp': 'LDA', 'gga': 'PS'},
                   # Meta-GGAs
                   'tpss': {'pp': 'PBE', 'metagga': 'TPSS'},
                   'revtpss': {'pp': 'PBE', 'metagga': 'RTPSS'},
                   'm06l': {'pp': 'PBE', 'metagga': 'M06L'},
                   # vdW-DFs
                   'optpbe-vdw': {'pp': 'LDA', 'gga': 'OR', 'luse_vdw': True,
                                  'aggac': 0.0},
                   'optb88-vdw': {'pp': 'LDA', 'gga': 'BO',  'luse_vdw': True,
                                  'aggac': 0.0, 'param1': 1.1/6.0,
                                  'param2': 0.22},
                   'optb86b-vdw': {'pp': 'LDA', 'gga': 'MK', 'luse_vdw': True,
                                   'aggac': 0.0, 'param1': 0.1234,
                                   'param2': 1.0},
                   'vdw-df2': {'pp': 'LDA', 'gga': 'ML', 'luse_vdw': True,
                               'aggac': 0.0, 'zab_vdw': -1.8867},
                   'beef-vdw': {'pp': 'PBE', 'gga': 'BF', 'luse_vdw': True,
                                'zab_vdw': -1.8867, 'lbeefens': True},
                   # hybrids
                   'pbe0': {'pp': 'LDA', 'gga': 'PE', 'lhfcalc': True},
                   'hse03': {'pp': 'LDA', 'gga': 'PE', 'lhfcalc': True,
                             'hfscreen': 0.3},
                   'hse06': {'pp': 'LDA', 'gga': 'PE', 'lhfcalc': True,
                             'hfscreen': 0.2},
                   'b3lyp': {'pp': 'LDA', 'gga': 'B3', 'lhfcalc': True,
                             'aexx': 0.2, 'aggax': 0.72,
                             'aggac': 0.81, 'aldac': 0.19},
                   'hf': {'pp': 'PBE', 'lhfcalc': True, 'aexx': 1.0,
                          'aldac': 0.0, 'aggac': 0.0}}

    default_parameters = dict(
        xc='PBE',
        pp='PBE',
        ismear=1,
        sigma=0.1,
        lwave=False,
        lcharg=False,
        kpts=[1, 1, 1])

    # These need to be kept separate for writing the incar.
    special_kwargs = ['xc',  # sets vasp tags for the exc-functional
                      'pp',  # determines where POTCARs are retrieved from
                      'setups',
                      # kpoints
                      'kpts',
                      'gamma',
                      'kpts_nintersections',
                      'reciprocal',
                      # DFT + U dictionary
                      'ldau_luj']

    # enumerated states
    EMPTY = 0
    NEW = 1
    QUEUED = 2
    FINISHED = 3
    NOTFINISHED = 4
    EMPTYCONTCAR = 5
    NEB = 10
    UNKNOWN = 100

    def __init__(self, label,
                 restart=True, ignore_bad_restart_file=False,
                 atoms=None, scratch=None,
                 debug=None,
                 exception_handler=VaspExceptionHandler,
                 **kwargs):
        """Create a Vasp calculator.

        label: the directory where the calculation files will be and
        the calculation run.

        debug: an integer, but usually something like logging.DEBUG

        exception_handler: A function for
        handling exceptions. The function should take the arguments
        returned by sys.exc_info(), which is the exception type, value
        and traceback. The default is VaspExceptionHandler.

        **kwargs
          Any Vasp keyword can be used, e.g. encut=450.

          The tag will be upcased when written, and the value is
          written depending on its type. E.g. integers, floats and
          strings are written as they are. True/False is written as
          .TRUE. and .FALSE. and Python lists/tuples are written as
          space delimited lists.

        Special kwargs:

        xc: string indicating the functional to use. It is expanded
        from Vasp.xc_defaults to the relevant Vasp tags.

        kpts: Usually a 3 element list of [k1, k2, k3], but may also
        be a list of kpts.

        setups: This describes special setups for the POTCARS. It is a list of
          the following items.

          (atom_index, suffix)   for exampe: (2, '_sv')

          (atom_symbol, suffix)  for example ('Zr', '_sv')

          If (atom_index, suffix) is used then only that atom index will have a
          POTCAR defined by '{}{}'.format(atoms[atom_index].symbol, suffix)

          If (atom_symbol, suffix) is used then atoms with that symbol (except
          any identified by (atom_index, suffix) will use a POTCAR defined by
          '{}{}'.format(atom_symbol, suffix)

          This syntax has changed from the old dictionary format. The
          reason for this is that this sorting must be
          deterministic. Getting keys in a dictionary is not
          deterministic.

        ldau_luj: This is a dictionary to set the DFT+U tags. For
        example, to put U=4 on the d-orbitals (L=2) of Cu, and nothing
        on the oxygen atoms in a calculation use:

            ldau_luj={'Cu':{'L':2,  'U':4.0, 'J':0.0},
                      'O':{'L':-1, 'U':0.0, 'J':0.0}},

        """
        self.set_label(label)  # set first so self.directory is right
        self.debug = debug
        self.exception_handler = exception_handler

        self.neb = None
        # We have to check for the type here this because an NEB uses
        # a list of atoms objects. We set pbc to be True because that
        # is what is read in from files, and if we don't the atoms
        # look incompatible.
        if atoms is not None and isinstance(atoms, ase.atoms.Atoms):
            atoms.pbc = [True, True, True]
        elif atoms is not None:
            for a in atoms:
                a.pbs = [True, True, True]
            self.neb = True

        # We do not pass kwargs here. Some of the special kwargs
        # cannot be set at this point since they need to know about
        # the atoms and parameters. This reads params and results from
        # existing files if they are there. It calls self.read(). It
        # should update the atoms from what is on file.

        if self.neb is not None:
            FileIOCalculator.__init__(self, restart, ignore_bad_restart_file,
                                      label)
            self.neb = atoms
        else:
            FileIOCalculator.__init__(self, restart, ignore_bad_restart_file,
                                      label, atoms)

        # The calculator should be up to date with the file
        # system here.

        # Add default parameters if they aren't set otherwise.
        for key, val in Vasp.default_parameters.iteritems():
            if key not in kwargs:
                kwargs[key] = val

        # Next we update kwargs with the special kwarg
        # dictionaries.
        if 'ispin' in kwargs:
            ispin = kwargs['ispin']
            del kwargs['ispin']
        else:
            ispin = None

        # if 'ldau_luj' in kwargs:
        #     kwargs.update(self.set_ldau_luj_dict(kwargs['ldau_luj']))

        # if 'xc' in kwargs:
        #     kwargs.update(self.set_xc_dict(kwargs['xc'].lower()))

        # if 'nsw' in kwargs:
        #     kwargs.update(self.set_nsw_dict(kwargs['nsw']))

        # Now update the parameters. If there are any new kwargs here,
        # it will reset the calculator and cause a calculation to be
        # run if needed.
        self.set(**kwargs)

        # In case no atoms was on file, and one is passed in, we set
        # it here.
        if self.atoms is None and atoms is not None and self.neb is None:
            self.sort_atoms(atoms)
        elif self.neb is not None:
            self.sort_atoms(self.neb[0])

        # This one depends on having atoms already.
        if ispin is not None:
            self.set(**self.set_ispin_dict(ispin))

        if VASPRC['validate']:
            for key, val in self.parameters.iteritems():
                if key in validate.__dict__:
                    f = validate.__dict__[key]
                    f(self, val)
                else:
                    warnings.warn('No validation for {}'.format(key))

    def sort_atoms(self, atoms=None):
        """Generate resort list, and make list of POTCARs to use.

        Returns None.

        """
        self.resort = None
        self.ppp_list = None
        self.symbol_count = None

        if atoms is None:
            log.debug('Atoms was none.')
            return
        self.atoms = atoms

        # Now we sort the atoms and generate the list of POTCARS
        # We end up with ppp = [(index_or_symbol, potcar_file, count)]
        # and resort_indices
        setups = self.parameters.get('setups', [])
        pp = self.parameters['pp']

        ppp = []  # [(index_or_symbol, potcar_file, count)]

        # indices of original atoms needed to make sorted atoms list
        resort_indices = []

        # First the numeric index setups
        for setup in [x for x in setups if isinstance(x[0], int)]:
            ppp += [[setup[0],
                     'potpaw_{}/{}{}/POTCAR'.format(pp, atoms[setup[0]].symbol,
                                                    setup[1]),
                     1]]
            resort_indices += [setup[0]]

        # now the rest of the setups. These are atom symbols
        for setup in [x for x in setups if not isinstance(x[0], int)]:
            symbol = setup[0]
            count = 0
            for i, atom in enumerate(atoms):
                if atom.symbol == symbol and i not in resort_indices:
                    count += 1
                    resort_indices += [i]

            ppp += [[symbol,
                     'potpaw_{}/{}{}/POTCAR'.format(pp, symbol, setup[1]),
                     count]]
        # now the remaining atoms use default potentials
        # First get the chemical symbols that remain
        symbols = []
        for atom in atoms or []:
            if (atom.symbol not in symbols and
                atom.symbol not in [x[0] for x in ppp]):
                symbols += [atom.symbol]

        for symbol in symbols:
            count = 0
            for i, atom in enumerate(atoms):
                if atom.symbol == symbol and i not in resort_indices:
                    resort_indices += [i]
                    count += 1
            if count > 0:
                ppp += [[symbol,
                         'potpaw_{}/{}/POTCAR'.format(pp, symbol),
                         count]]

        assert len(resort_indices) == len(atoms), \
            'Sorting error. sort_indices={}'.format(resort_indices)

        assert sum([x[2] for x in ppp]) == len(atoms)

        self.resort = resort_indices
        self.ppp_list = ppp
        self.atoms_sorted = atoms[self.resort]
        self.symbol_count = [(x[0] if isinstance(x[0], str)
                              else atoms[x[0]].symbol,
                              x[2]) for x in ppp]

        return atoms[self.resort]

    def __str__(self):
        """Pretty representation of a calculation.

        TODO: make more like jaspsum.

        """
        s = ['']
        s += ['Vasp calculation in {self.directory}\n']
        if os.path.exists(self.incar):
            with open(self.incar) as f:
                s += [f.read()]
        else:
            s += ['No INCAR yet']

        if os.path.exists(self.poscar):
            with open(self.poscar) as f:
                s += [f.read()]
        else:
            s += ['No POSCAR yet']

        return '\n'.join(s).format(self=self)

    def set_label(self, label):
        """Set working directory.

        In VASP there is no prefix, only the working directory.

        """

        if label is None:
            self.directory = os.path.abspath(".")
            self.prefix = None
        else:
            d = os.path.expanduser(label)
            d = os.path.abspath(d)
            self.directory, self.prefix = d, None
            if not os.path.isdir(self.directory):
                os.makedirs(self.directory)

        # Convenient attributes for file names
        for f in ['INCAR', 'POSCAR', 'CONTCAR', 'POTCAR',
                  'KPOINTS', 'OUTCAR']:
            fname = os.path.join(self.directory, f)
            setattr(self, f.lower(), fname)

    def check_state(self, atoms=None):
        """Check if any changes exist that require new calculations."""
        if atoms is None:
            atoms = self.get_atoms()

        system_changes = FileIOCalculator.check_state(self, atoms)
        # Ignore boundary conditions:
        if 'pbc' in system_changes:
            system_changes.remove('pbc')

        # if dir is empty, there is nothing to read here.
        if self.get_state() == Vasp.EMPTY:
            return system_changes

        # Check if the parameters have changed
        file_params = {}
        file_params.update(self.read_incar())
        file_params.update(self.read_potcar())
        file_params.update(self.read_kpoints())

        xc_keys = sorted(Vasp.xc_defaults,
                         key=lambda k: len(Vasp.xc_defaults[k]),
                         reverse=True)

        for ex in xc_keys:
            pd = {k: file_params.get(k, None)
                  for k in Vasp.xc_defaults[ex]}
            if pd == Vasp.xc_defaults[ex]:
                file_params['xc'] = ex
                break

        # reconstruct ldau_luj if necessary
        if 'ldauu' in file_params:
            ldaul = file_params['ldaul']
            ldauj = file_params['ldauj']
            ldauu = file_params['ldauu']

            with open(self.potcar) as f:
                lines = f.readlines()

            # symbols are in the first line of each potcar
            symbols = [lines[0].split()[1]]
            for i, line in enumerate(lines):
                if 'End of Dataset' in line and i != len(lines) - 1:
                    symbols += [lines[i + 1].split()[1]]

            ldau_luj = {}
            for sym, l, j, u in zip(symbols, ldaul, ldauj, ldauu):
                ldau_luj[sym] = {'L': l, 'U': u, 'J': j}

            file_params['ldau_luj'] = ldau_luj

        if not self.parameters == file_params:
            new_keys = set(self.parameters.keys()) - set(file_params.keys())
            missing_keys = (set(file_params.keys()) -
                            set(self.parameters.keys()))

            log.debug('New keys: {}'.format(new_keys))
            log.debug('Missing keys: {}'.format(missing_keys))
            system_changes += ['params_on_file']

        return system_changes

    def reset(self):
        """overwrite to avoid killing self.atoms."""
        self.results = {}

    def update(self, atoms=None):
        """Updates calculator.

        If a calculation is required,  run it, otherwise updates results.

        """
        if atoms is None:
            atoms = self.get_atoms()

        if self.neb:
            return self.get_neb()

        if self.calculation_required(atoms, ['energy']):
            return self.calculate(atoms)
        else:
            self.read_results()

        return True

    def calculation_required(self, atoms=None, properties=['energy']):
        """Returns if a calculation is needed."""

        if atoms is None:
            atoms = self.get_atoms()

        system_changes = self.check_state(atoms)
        if system_changes:
            log.debug('Calculation needed for {}'.format(system_changes))
            return True

        for name in properties:
            if name not in self.results:
                log.debug('{} not in {}. Calc required.'.format(name,
                                                                self.results))
                return True

        # if the calculation is finished we do not need to run.
        if os.path.exists(self.outcar):
            with open(self.outcar) as f:
                lines = f.readlines()
                if 'Voluntary context switches:' in lines[-1]:
                    return False

    def calculate(self, atoms=None, properties=['energy'],
                  system_changes=None):
        """Runs a calculation, only if necessary."""
        if self.calculation_required(atoms, properties):

            # The subclass implementation should first call this
            # implementation to set the atoms attribute.
            Calculator.calculate(self, atoms, properties, system_changes)

            self.write_input(atoms, properties, system_changes)

            if self.command is None:
                raise RuntimeError('Please set $%s environment variable ' %
                                   ('ASE_' + self.name.upper() + '_COMMAND') +
                                   'or supply the command keyword')

            olddir = os.getcwd()
            try:
                os.chdir(self.directory)
                errorcode = subprocess.call(self.command,
                                            stdout=subprocess.PIPE,
                                            shell=True)

            finally:
                os.chdir(olddir)

            if errorcode:
                s = '{} returned an error: {}'
                raise RuntimeError(s.format(self.name, errorcode))

        # This sets self.results, and updates the atoms
        self.read_results()

    def abort(self):
        """Abort and exit the program the calculator is running in."""
        import sys
        sys.exit()

    def stop_if(self, condition=None):
        """Stop program if condition is truthy."""
        if condition:
            import sys
            sys.exit()

    def wait(self):
        """Stop program if not ready."""
        self.stop_if(self.potential_energy is None)

    def run(self):
        """Convenience function to run calculation."""
        return self.potential_energy

    def clone(self, newdir):
        """Copy the calculation directory to newdir and set label to
        newdir.

        """
        state = self.get_state()

        import shutil
        if not os.path.isdir(newdir):
            shutil.copytree(self.directory, newdir)

            # need some cleanup here. do not copy jobids, etc...
            # What survives depends on the state
            # delete these files if not finished.
            if state in [Vasp.QUEUED, Vasp.NOTFINISHED]:
                os.unlink(os.path.join(newdir, 'OUTCAR'))
                os.unlink(os.path.join(newdir, 'vasprun.xml'))

            if state in [Vasp.EMPTYCONTCAR]:
                os.unlink(os.path.join(newdir, 'OUTCAR'))
                os.unlink(os.path.join(newdir, 'vasprun.xml'))
                os.unlink(os.path.join(newdir, 'CONTCAR'))

        self.__init__(newdir)
        self.write_db(jobid=None, path=newdir)

    def get_state(self):
        """Determine calculation state based on directory contents.

        Returns an integer for the state.

        """

        base_input = [os.path.exists(os.path.join(self.directory, f))
                      for f in ['INCAR', 'POSCAR', 'POTCAR', 'KPOINTS']]

        # Check for NEB first.
        if (np.array([os.path.exists(os.path.join(self.directory, f))
                      for f in ['INCAR', 'POTCAR', 'KPOINTS']]).all()
            and not os.path.exists(os.path.join(self.directory, 'POSCAR'))
            and os.path.isdir(os.path.join(self.directory, '00'))):
            return Vasp.NEB

        # Some input does not exist
        if False in base_input:
            # some input file is missing
            return Vasp.EMPTY

        # Input files exist, but no jobid, and no output
        if (np.array(base_input).all()
            and self.get_db('jobid') is not None
            and not os.path.exists(os.path.join(self.directory, 'OUTCAR'))):
            return Vasp.NEW

        # INPUT files exist, a jobid in the queue
        if self.in_queue():
            return Vasp.QUEUED

        # Not in queue, and finished
        if not self.in_queue():
            if os.path.exists(self.outcar):
                with open(self.outcar) as f:
                    lines = f.readlines()
                    if 'Voluntary context switches:' in lines[-1]:
                        return Vasp.FINISHED

        # Not in queue, and not finished
        if not self.in_queue():
            if os.path.exists(self.outcar):
                with open(self.outcar) as f:
                    lines = f.readlines()
                    if 'Voluntary context switches:' not in lines[-1]:
                        return Vasp.NOTFINISHED
            else:
                return Vasp.NOTFINISHED

        # Not in queue, and not finished, with empty contcar
        if not self.in_queue():
            if os.path.exists(self.contcar):
                with open(self.contcar) as f:
                    if f.read() == '':
                        return Vasp.EMPTYCONTCAR

        return Vasp.UNKNOWN

    @property
    def potential_energy(self):
        """Property to return potential_energy."""
        self.update()
        atoms = self.get_atoms()
        return atoms.get_potential_energy()

    @property
    def forces(self, apply_constraints=False):
        """Property to return forces."""
        self.update()
        atoms = self.get_atoms()
        return atoms.get_forces(apply_constraints)

    @property
    def stress(self):
        """Property to return stress."""
        self.update()
        atoms = self.get_atoms()
        return atoms.get_stress()

    @property
    def traj(self):
        """Get a trajectory.

        This reads Atoms objects from vasprun.xml. By default returns
        all images.  If index is an integer, return that image.

        Technically, this is just a list of atoms with
        SinglePointCalculator attached to them.

        This is usually only relevant if you have done a
        relaxation. If the calculation is an NEB, the images are
        returned.

        """
        self.update()

        if self.neb:
            from ase.calculators.singlepoint import SinglePointCalculator
            images, energies = self.get_neb()
            tatoms = [x.copy() for x in images]
            for i, x in enumerate(tatoms):
                x.set_calculator(SinglePointCalculator(x, energy=energies[i]))
            return tatoms

        LOA = []
        i = 0
        while True:
            try:
                atoms = read_vasp_xml(os.path.join(self.directory,
                                                   'vasprun.xml'),
                                      index=i).next()
                LOA += [atoms]
                i += 1
            except IndexError:
                break
        return LOA

    def view(self, index=None):
        """Visualize the calculation.

        """
        from ase.visualize import view
        if index is not None:
            return view(self.traj[index])
        else:
            return view(self.traj)


    def describe(self, long=False):
        """Describe each parameters used from the docstrings in vasp.validate."""
        for key in sorted(self.parameters.keys()):
            if key in validate.__dict__:
                f = validate.__dict__[key]
                d = f.__doc__ or 'No docstring found.'
                print('{} = {}:'.format(key, self.parameters[key]))
                if long:
                    print('  ' + d)
                else:
                    print('  ' + d.split('\n')[0])
                print('')
